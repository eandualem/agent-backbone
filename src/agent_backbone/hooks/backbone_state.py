#!/usr/bin/env python3
"""What every backbone hook script shares: the state vocabulary, where the
state file lives, how it is written, and the ``gh issue comment`` detector.

Standard library only — copied next to the hook scripts into
``<data_dir>/hooks/`` so they run under any ``python3``. Each runtime's
script (``claude_hook.py``, ``codex_hook.py``, ``gemini_hook.py``) maps
that CLI's events onto the shared states and hands the record to
``run_hook``.

States written (runtime-agnostic vocabulary):

    idle                       at the prompt, nothing running
    busy                       working on a prompt
    waiting_for_human (reason) plan | permission | question
    unknown                    the session ended
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

STATE_IDLE = "idle"
STATE_BUSY = "busy"
STATE_WAITING = "waiting_for_human"
STATE_BLOCKED = "blocked"
STATE_UNKNOWN = "unknown"

REASON_PLAN = "plan"
REASON_PERMISSION = "permission"
REASON_QUESTION = "question"
REASON_QUOTA = "quota"

LAST_MESSAGE_CHARS = 500

_ISSUE_NUMBER_RE = re.compile(r"(?:#|\bissue[\s:#]*)(\d{1,7})\b", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{1,7})\b")

Derive = Callable[[dict, "dict | None"], "tuple[dict | None, dict | list[dict] | None]"]


def resolve_agent(explicit: str | None) -> str | None:
    """Agent name from CLI flag, environment, or the enclosing tmux session."""
    if explicit:
        return explicit
    env_name = os.environ.get("BACKBONE_AGENT", "").strip()
    if env_name:
        return env_name
    if os.environ.get("TMUX"):
        try:
            out = subprocess.run(
                ["tmux", "display-message", "-p", "#S"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            name = out.stdout.strip()
            if out.returncode == 0 and name:
                return name
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def issue_from_text(text: str) -> tuple[int | None, str | None]:
    """``(number, repo)`` mentioned in a prompt, e.g. ``owner/name#42`` or ``issue #42``."""
    ref = _ISSUE_REF_RE.search(text or "")
    if ref:
        return int(ref.group(2)), ref.group(1)
    for match in _ISSUE_NUMBER_RE.finditer(text or ""):
        # CSS hex colors (including all-digit colors) are not task IDs.
        if match.group(0).startswith("#") and len(match.group(1)) in {3, 4, 6, 8}:
            prefix = (text or "")[: match.start()].lower()[-40:]
            if not re.search(r"(?:issue|pr|pull request)\s*$", prefix) and re.search(
                r"(?:color|background|foreground|theme|fill|stroke|hex)\b[\"\']?"
                r"(?:\s+(?:is|to|as|of|value)){0,2}\s*(?:[:=]\s*)?[`\"\']?$",
                prefix,
            ):
                continue
        return int(match.group(1)), None
    return None, None


def issue_from_prompt(prompt: str, current: dict) -> tuple[int | None, str | None]:
    """The issue a prompt is about, else what the previous record said."""
    issue, repo = current.get("issue"), current.get("repo")
    if "issue" in (prompt or "").lower() or "#" in (prompt or ""):
        found, found_repo = issue_from_text(prompt)
        if found is not None:
            issue = found
            if found_repo:
                repo = found_repo
    return issue, repo


def command_argv(command: str | list[str]) -> list[list[str]]:
    """Parse direct commands and success-chained commands without evaluating shell text.

    Ambiguous shell control flow/expansion is left to GitHub confirmation. Quoted
    operators stay arguments; argv lists retain their original argument boundaries.
    """
    if isinstance(command, list):
        if not all(isinstance(part, str) for part in command):
            return []
        if (
            len(command) == 3
            and Path(command[0]).name in {"sh", "bash", "zsh"}
            and command[1] in {"-c", "-lc"}
        ):
            return command_argv(command[2])
        return [command] if command else []
    if not isinstance(command, str):
        return []
    parts, start, quote, escaped = [], 0, "", False
    index = 0
    command = command.strip()
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char in "$`":
                return []
        elif char in "\"'":
            quote = char
        elif command[index : index + 2] == "&&":
            parts.append(command[start:index])
            index += 1
            start = index + 1
        elif char in ";|&()<>\n$`#":
            return []
        index += 1
    parts.append(command[start:])
    try:
        parsed = [shlex.split(part) for part in parts]
    except ValueError:
        return []
    control = {
        "exit",
        "return",
        "exec",
        "eval",
        "source",
        ".",
        "break",
        "continue",
        "command",
        "builtin",
        "trap",
        "alias",
        "unalias",
        "shopt",
        "set",
    }
    return parsed if all(argv and argv[0] not in control for argv in parsed) else []


def _gh_arguments(argv: list[str]) -> tuple[list[str], dict[str, str]] | None:
    """Separate supported gh operands from option values (a body may contain numbers)."""
    values = {
        "-R": "repo",
        "--repo": "repo",
        "-b": "body",
        "--body": "body",
        "-F": "body-file",
        "--body-file": "body-file",
        "-H": "head",
        "--head": "head",
        "-B": "base",
        "--base": "base",
        "-t": "title",
        "--title": "title",
        "-a": "assignee",
        "--assignee": "assignee",
        "-l": "label",
        "--label": "label",
        "-p": "project",
        "--project": "project",
        "-r": "reviewer",
        "--reviewer": "reviewer",
        "-T": "template",
        "--template": "template",
        "--recover": "recover",
    }
    flags = {
        "--draft",
        "-d",
        "--fill",
        "--fill-first",
        "--fill-verbose",
        "--edit-last",
        "--create-if-none",
    }
    positional, options = [], {}
    args = iter(argv)
    for arg in args:
        key, separator, value = arg.partition("=")
        if key in values:
            value = value if separator else next(args, None)
            if value is None:
                return None
            options[values[key]] = value
        elif arg in flags:
            continue
        elif arg.startswith("-"):
            return None
        else:
            positional.append(arg)
    return positional, options


def comment_action_from_argv(argv: list[str], now: float) -> dict | None:
    if (
        len(argv) < 3
        or Path(argv[0]).name != "gh"
        or argv[1:3] not in (["issue", "comment"], ["pr", "comment"])
    ):
        return None
    parsed = _gh_arguments(argv[3:])
    if parsed is None:
        return None
    operands, options = parsed
    if len(operands) != 1 or not operands[0].isdigit():
        return None
    action = {"ts": now, "action": "comment", "issue": int(operands[0])}
    if options.get("repo"):
        action["repo"] = options["repo"]
    return action


def comment_action_from_mcp(tool: str, tool_input: dict, now: float) -> dict | None:
    """The GitHub MCP server's ``add_issue_comment`` is the same acknowledgement."""
    if not (tool.startswith("mcp__") and tool.endswith("__add_issue_comment")):
        return None
    raw = tool_input.get("issue_number") or tool_input.get("issueNumber")
    try:
        number = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        number = None
    if number is None:
        return None
    action = {"ts": now, "action": "comment", "issue": number}
    owner, name = tool_input.get("owner"), tool_input.get("repo")
    if owner and name:
        action["repo"] = f"{owner}/{name}"
    return action


_REMOTE_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def _git_output(cwd: str | None, *args: str) -> str | None:
    if not cwd:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def pull_request_action_from_argv(argv: list[str], cwd: str | None, now: float) -> dict | None:
    """A ``gh pr create`` in a shell command: the backbone should not announce
    that pull request back to the agent that opened it, and the issues it
    closes count as acknowledged.

    Records what identifies the pull request in GitHub's event: the **head**
    repository (the checkout's ``origin`` — a fork when working from one;
    ``--head owner:branch`` names the owner) and the head branch. ``repo``
    is the base repository when ``--repo`` names one, else the origin.
    """
    if len(argv) < 3 or Path(argv[0]).name != "gh" or argv[1:3] != ["pr", "create"]:
        return None
    parsed = _gh_arguments(argv[3:])
    if parsed is None or parsed[0]:
        return None
    options = parsed[1]
    remote = _git_output(cwd, "remote", "get-url", "origin")
    found = _REMOTE_RE.search(remote or "")
    origin = found.group(1) if found else None
    repo = options.get("repo") or origin
    head = options.get("head")
    head_repo = origin
    if head:
        owner, colon, branch = head.rpartition(":")
        if colon and owner:
            # gh's "owner:branch" form: that owner's fork, same repository name.
            base_name = (repo or origin or "").rsplit("/", 1)[-1]
            head_repo = f"{owner}/{base_name}" if base_name else None
    else:
        branch = _git_output(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    action = {"ts": now, "action": "pull_request"}
    if repo:
        action["repo"] = repo
    if head_repo:
        action["head_repo"] = head_repo
    if branch:
        action["branch"] = branch
    return action


def tool_actions(tool: str, tool_input: dict, cwd: str | None, now: float) -> list[dict]:
    """Parse only shell tool arguments or the GitHub MCP comment operation."""
    if not isinstance(tool_input, dict):
        return []
    mcp = comment_action_from_mcp(tool, tool_input, now)
    if mcp:
        return [mcp]
    if tool not in {"Bash", "shell", "shell_command", "exec_command", "run_shell_command"}:
        return []
    effective_cwd = cwd
    # Codex shell tools can run elsewhere without changing the hook event cwd.
    for key in ("workdir", "cwd"):
        if key in tool_input:
            override = tool_input[key]
            if not isinstance(override, str) or not override.strip():
                return []
            directory = Path(override)
            if not directory.is_absolute():
                if not cwd:
                    return []
                directory = Path(cwd) / directory
            effective_cwd = str(directory.resolve())
            break
    return shell_actions(tool_input.get("command", tool_input.get("cmd", "")), effective_cwd, now)


def effective_gh_repo(cwd: str | None) -> str | None:
    """Resolve safe local gh selection; ambiguous defaults wait for GitHub confirmation."""
    if os.environ.get("GH_HOST", "github.com").casefold() != "github.com":
        return None
    override = os.environ.get("GH_REPO")
    if override:
        parts = override.split("/")
        if len(parts) == 3 and parts[0].casefold() == "github.com":
            parts = parts[1:]
        if len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
            return "/".join(parts)
        return None
    defaults = _git_output(cwd, "config", "--get-regexp", r"^remote\..*\.gh-resolved$")
    if any(re.match(r"remote\..*\.gh-resolved\s+", line) for line in (defaults or "").splitlines()):
        # gh owns this resolution protocol. Do not guess an origin-based
        # acknowledgment when it selected a default remote; its API confirms it.
        return None
    remote = _git_output(cwd, "remote", "get-url", "origin")
    found = _REMOTE_RE.search(remote or "")
    return found.group(1) if found else None


def shell_actions(command: str | list[str], cwd: str | None, now: float) -> list[dict]:
    actions = []
    for argv in command_argv(command):
        action = comment_action_from_argv(argv, now) or pull_request_action_from_argv(
            argv, cwd, now
        )
        if action:
            if not action.get("repo") and (repo := effective_gh_repo(cwd)):
                action["repo"] = repo
            actions.append(action)
        elif argv and argv[0] == "cd":
            # Later gh commands run elsewhere; never infer repo/branch from the old cwd.
            if len(argv) != 2 or cwd is None:
                return []
            cwd = str((Path(cwd) / argv[1]).resolve())
    return actions


def action_records(payload: dict, now: float, *, phase: str) -> list[dict]:
    """Stamp intent or confirmed success separately so only success acknowledges."""
    return [
        {**action, "phase": phase}
        for action in tool_actions(
            payload.get("tool_name", "") or "",
            payload.get("tool_input") or {},
            payload.get("cwd"),
            now,
        )
    ]


def response_succeeded(response: object) -> bool:
    """Require explicit completion evidence; missing/streaming/error output is unknown."""
    if not isinstance(response, dict):
        return False
    if any(response.get(key) for key in ("isError", "is_error", "error", "interrupted")):
        return False
    if response.get("success") is False:
        return False
    for key in ("exit_code", "exitCode"):
        if key in response:
            return type(response[key]) is int and response[key] == 0
    return response.get("success") is True or response.get("isError") is False


def plan_title(plan: str) -> str:
    for line in plan.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "Untitled plan"


TRANSCRIPT_TAIL_BYTES = 256 * 1024


def observed_model(payload: dict, current: dict | None, event: str) -> str | None:
    """The model the runtime is actually answering with, when it can be known.

    No CLI puts the model in its hook payload today (Claude Code 2.1.267
    measured 2026-09-10), but Claude Code names the transcript, and every
    assistant entry there carries ``"model": "…"``. The tail of that file is
    read on the events that follow a reply; other events carry the last
    observation forward. A payload ``model`` field, if one appears, wins.
    """
    current = current or {}
    direct = payload.get("model")
    if isinstance(direct, str) and direct.strip() and not direct.startswith("<"):
        return direct.strip()
    if event in ("Stop", "SessionStart", "UserPromptSubmit"):
        transcript = payload.get("transcript_path")
        if isinstance(transcript, str) and transcript:
            found = _model_from_transcript(Path(transcript))
            if found:
                return found
    return current.get("model") or None


def _model_from_transcript(path: Path) -> str | None:
    """The last Claude assistant record's model, never a nested tool argument."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - TRANSCRIPT_TAIL_BYTES)
            stream.seek(start)
            tail = stream.read().decode("utf-8", "replace")
    except OSError:
        return None
    if start:
        tail = tail.partition("\n")[2]  # the first record may be truncated
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and 0 < len(model) <= 120 and not any(c in model for c in "<>"):
            return model
    return None


def record_factory(payload: dict, current: dict | None, event: str) -> Callable[..., dict]:
    """A ``state(new_state, reason=None, **extra)`` builder that keeps ``issue``,
    ``repo`` and ``started_at`` stable across events and stamps the runtime's
    session id, the observed model and the event that produced the record.
    ``prompted_at`` is when the runtime last took a prompt (``UserPromptSubmit``),
    kept through the turn's later records: delivery's receipt for a paste."""
    now = time.time()
    current = current or {}
    session_id = payload.get("session_id") or current.get("session_id")
    runtime = os.environ.get("BACKBONE_RUNTIME", "").strip() or current.get("runtime")
    model = observed_model(payload, current, event)

    def state(new_state: str, reason: str | None = None, **extra) -> dict:
        record = {
            "state": new_state,
            "reason": reason,
            "issue": current.get("issue"),
            "repo": current.get("repo"),
            "ts": now,
            "started_at": current.get("started_at") or now,
            "event": event,
        }
        if session_id:
            record["session_id"] = session_id
        if launch_id := os.environ.get("BACKBONE_LAUNCH_ID"):
            record["launch_id"] = launch_id
        if runtime:
            record["runtime"] = runtime
        if model:
            record["model"] = model
        if current.get("last_message") is not None:
            record["last_message"] = current["last_message"]
        prompted_at = now if event == "UserPromptSubmit" else current.get("prompted_at")
        if prompted_at is not None:
            record["prompted_at"] = prompted_at
        record.update(extra)
        return record

    return state


def clip_message(text: str | None) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    return text[:LAST_MESSAGE_CHARS] + ("…" if len(text) > LAST_MESSAGE_CHARS else "")


def write_state(state_dir: Path, agent: str, record: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    plan_text = record.pop("plan_text", None)
    if plan_text is not None:
        plans_dir = state_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plans_dir / f"{agent}.md"
        plan_path.write_text(plan_text)
        record["plan_file"] = str(plan_path)
    target = state_dir / f"{agent}.json"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")  # never shared with another writer
    tmp.write_text(json.dumps(record))
    os.replace(tmp, target)


def remember_usage_session(state_dir: Path, agent: str, record: dict) -> None:
    """Keep session identity after the next start replaces the current state file."""
    runtime, session_id = record.get("runtime"), record.get("session_id")
    if not isinstance(runtime, str) or not isinstance(session_id, str):
        return
    if not runtime or not session_id or len(runtime) > 100 or len(session_id) > 300:
        return
    launch = record.get("launch_id")
    identity = hashlib.sha256(
        f"{agent}\0{runtime}\0{session_id}\0{launch or chr(45)}".encode()
    ).hexdigest()
    directory = state_dir / "usage-sessions"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (identity + ".json")
    if target.exists():
        return  # identity is immutable; collection consumes it after the DB commit
    value = {
        "agent": agent,
        "runtime": runtime,
        "session_id": session_id,
        "observed_at": record.get("ts") or time.time(),
    }
    if launch:
        value["launch_id"] = launch
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, target)


def append_action(state_dir: Path, agent: str, action: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "actions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**action, "session": agent}) + "\n")


CONTEXT_DIR = "context"
"""``<state_dir>/context/<agent>/`` — text the backbone offers a *working*
agent through its runtime's hook, because nothing is ever pasted into a busy
terminal. The backbone writes ``<key>.md``; whoever renames it first owns it:
the hook renames it to ``<key>.taken`` and returns the text as hook context,
the backbone renames it to ``<key>.claimed`` when it is about to paste the
same message at the prompt instead. ``clear_context`` removes what is left."""


def _context_dir(state_dir: Path, agent: str) -> Path:
    return state_dir / CONTEXT_DIR / agent


def offer_context(state_dir: Path, agent: str, key: str, text: str) -> bool:
    """Backbone side: offer ``text`` under ``key``. False when the hook already took it."""
    directory = _context_dir(state_dir, agent)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / f"{key}.taken").exists():
        return False
    target = directory / f"{key}.md"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return True


def claim_context(state_dir: Path, agent: str, key: str) -> str:
    """Backbone side, before pasting: ``claimed`` (the offer is withdrawn),
    ``taken`` (the hook delivered it; do not paste) or ``missing`` (never offered)."""
    directory = _context_dir(state_dir, agent)
    try:
        os.rename(directory / f"{key}.md", directory / f"{key}.claimed")
    except FileNotFoundError:
        return "taken" if (directory / f"{key}.taken").exists() else "missing"
    (directory / f"{key}.claimed").unlink(missing_ok=True)
    return "claimed"


def clear_context(state_dir: Path, agent: str, key: str) -> None:
    directory = _context_dir(state_dir, agent)
    for suffix in ("md", "taken", "claimed"):
        (directory / f"{key}.{suffix}").unlink(missing_ok=True)


def clear_agent_context(state_dir: Path, agent: str) -> None:
    """Backbone side, before a new session: drop every offer left for the
    previous one. An offer is meant for the session it was written for; the
    queue row behind a batch survives and is pasted at the new prompt."""
    shutil.rmtree(_context_dir(state_dir, agent), ignore_errors=True)


def take_context(state_dir: Path, agent: str, launch_id: str | None = None) -> list[str]:
    """Hook side: claim every offered text (oldest first) and return it.

    Offers for the agent (subscription batches) come first, then the offers
    scoped to this session (steers under ``<agent>/<launch_id>/``, see
    ``offer_steer``); ``launch_id`` defaults to the session's own
    ``BACKBONE_LAUNCH_ID``, so a steer written for another session of the
    same agent is never taken here."""
    launch_id = launch_id or os.environ.get("BACKBONE_LAUNCH_ID", "").strip()
    directories = [_context_dir(state_dir, agent)]
    if launch_id:
        directories.append(_steer_dir(state_dir, agent, launch_id))
    texts: list[str] = []
    for directory in directories:
        try:
            # Queue ids are monotonic; no stat, so a file the backbone claims
            # meanwhile cannot abort the whole take.
            offers = sorted(
                directory.glob("*.md"),
                key=lambda path: (
                    not path.stem.isdigit(),
                    int(path.stem) if path.stem.isdigit() else 0,
                    path.stem,
                ),
            )
        except OSError:
            continue
        for offer in offers:
            taken = offer.with_suffix(".taken")
            try:
                # Read before renaming: once it is ``.taken`` the backbone may
                # settle and remove it.
                text = offer.read_text(encoding="utf-8")
                os.rename(offer, taken)
                texts.append(text)
            except OSError:
                continue  # the backbone claimed it first, or it is already gone
    return texts


STEER_PREFIX = "steer-"
"""Offer files under ``<agent>/<launch_id>/`` are ``steer-<8-digit delivery id>``:
a namespace of their own, so they never collide with a batch's numeric key."""


def _steer_dir(state_dir: Path, agent: str, launch_id: str) -> Path:
    return _context_dir(state_dir, agent) / launch_id


def steer_key(delivery_id: int) -> str:
    return f"{STEER_PREFIX}{delivery_id:08d}"


def offer_steer(state_dir: Path, agent: str, launch_id: str, key: str, text: str) -> bool:
    """Backbone side: offer a steer to one session of the agent. False when
    that key was already taken."""
    directory = _steer_dir(state_dir, agent, launch_id)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / f"{key}.taken").exists():
        return False
    target = directory / f"{key}.md"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return True


def retire_steers(state_dir: Path, agent: str, launch_id: str | None = None) -> None:
    """Hook side, when the turn ends: steers still offered to this session
    are for the task that just ended, never the next one; mark them
    ``.missed`` for the backbone to record as ``not_taken``."""
    launch_id = launch_id or os.environ.get("BACKBONE_LAUNCH_ID", "").strip()
    if not launch_id:
        return
    try:
        offers = list(_steer_dir(state_dir, agent, launch_id).glob(f"{STEER_PREFIX}*.md"))
    except OSError:
        return
    for offer in offers:
        with suppress(OSError):
            os.rename(offer, offer.with_suffix(".missed"))


SteerOffer = tuple[str, str, int, str, float]
"""``(agent, launch_id, delivery_id, state, age_seconds)`` of one steer file."""


def steer_offers(state_dir: Path, agent: str | None = None) -> list[SteerOffer]:
    """Backbone side: every steer offer on disk as
    ``(agent, launch_id, delivery_id, state, age_seconds)``; ``state`` is
    ``offered`` (``.md``), ``taken`` (``.taken``) or ``missed`` (``.missed``,
    the turn ended first)."""
    root = state_dir / CONTEXT_DIR
    found: list[SteerOffer] = []
    now = time.time()
    try:
        agents = [root / agent] if agent else [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return found
    for agent_dir in agents:
        try:
            launches = [p for p in agent_dir.iterdir() if p.is_dir()]
        except OSError:
            continue
        for launch_dir in launches:
            try:
                files = [
                    path
                    for suffix in ("md", "taken", "missed")
                    for path in launch_dir.glob(f"{STEER_PREFIX}*.{suffix}")
                ]
            except OSError:
                continue
            for path in files:
                digits = path.stem[len(STEER_PREFIX) :]
                if not digits.isdigit():
                    continue
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                state = {".taken": "taken", ".missed": "missed"}.get(path.suffix, "offered")
                found.append((agent_dir.name, launch_dir.name, int(digits), state, age))
    return found


def expire_steer(state_dir: Path, agent: str, launch_id: str, delivery_id: int) -> bool:
    """Backbone side: claim an offer nobody took, atomically against the
    hook's own rename. False when the hook took it first."""
    offer = _steer_dir(state_dir, agent, launch_id) / f"{steer_key(delivery_id)}.md"
    try:
        os.rename(offer, offer.with_suffix(".missed"))
    except OSError:
        return False
    return True


def clear_steer(state_dir: Path, agent: str, launch_id: str, delivery_id: int) -> None:
    directory = _steer_dir(state_dir, agent, launch_id)
    for suffix in ("md", "taken", "missed"):
        (directory / f"{steer_key(delivery_id)}.{suffix}").unlink(missing_ok=True)


def hook_context_output(event: str, texts: list[str]) -> str:
    """The JSON a Claude Code / Codex hook prints to add ``texts`` to the model's context."""
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "\n\n".join(texts)}}
    )


def read_current(state_dir: Path, agent: str) -> dict | None:
    try:
        return json.loads((state_dir / f"{agent}.json").read_text())
    except (OSError, ValueError):
        return None


def run_hook(
    derive: Derive,
    argv: list[str] | None = None,
    *,
    context_events: frozenset[str] = frozenset(),
    turn_end_events: frozenset[str] = frozenset(),
) -> int:
    """Read the CLI's JSON payload from stdin, derive the state, write it.

    On an event in ``context_events`` (the CLI's hook events whose JSON output
    may add context to the model) the hook also hands over whatever the
    backbone offered under ``<state_dir>/context/<agent>/`` — see ``CONTEXT_DIR``.
    On an event in ``turn_end_events`` it retires this session's open steers.

    Usage (as configured by the installer):
        <script> --state-dir /path/to/state [--agent NAME]

    ``BACKBONE_STATE_DIR`` and ``BACKBONE_AGENT`` (exported into every
    session the backbone starts) win over the flags. Without an agent name
    or a state directory the hook exits silently; a hook must never make
    the CLI fail.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--tag", default=None, help="marker used by the installer; ignored")
    args = parser.parse_args(argv)

    agent = resolve_agent(args.agent)
    if not agent:
        return 0
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    raw_state_dir = os.environ.get("BACKBONE_STATE_DIR", "").strip() or args.state_dir
    if not raw_state_dir:
        return 0
    state_dir = Path(raw_state_dir).expanduser()
    try:
        record, action = derive(payload, read_current(state_dir, agent))
        if record is not None:
            write_state(state_dir, agent, record)
            with suppress(OSError):  # history must not suppress acknowledgements
                remember_usage_session(state_dir, agent, record)
        for entry in action if isinstance(action, list) else ([action] if action else []):
            append_action(state_dir, agent, entry)
        event = payload.get("hook_event_name", "")
        if event in context_events:
            texts = take_context(state_dir, agent)
            if texts:
                print(hook_context_output(event, texts))
        if event in turn_end_events:
            retire_steers(state_dir, agent)
    except Exception:  # a hook must never make the CLI fail
        # An unexpected payload shape or an unwritable state dir: the
        # backbone falls back to the terminal; the agent is not disturbed.
        return 0
    return 0


if __name__ == "__main__" and sys.argv[1:] == ["--shell-actions"]:
    # The OpenCode plugin delegates parsing without importing the package.
    request = json.load(sys.stdin)
    phase = request.get("phase")
    if phase not in {"intent", "succeeded"}:
        raise ValueError("unknown action phase")
    print(
        json.dumps(
            [
                {**action, "phase": phase}
                for action in shell_actions(
                    request.get("command", ""), request.get("cwd"), time.time()
                )
            ]
        )
    )
