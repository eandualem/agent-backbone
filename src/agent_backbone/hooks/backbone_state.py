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
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

STATE_IDLE = "idle"
STATE_BUSY = "busy"
STATE_WAITING = "waiting_for_human"
STATE_UNKNOWN = "unknown"

REASON_PLAN = "plan"
REASON_PERMISSION = "permission"
REASON_QUESTION = "question"

LAST_MESSAGE_CHARS = 500

_GH_COMMENT_RE = re.compile(r"\bgh\s+issue\s+comment\s+(?:\S+\s+)*?(\d+)\b")
_GH_REPO_RE = re.compile(r"(?:--repo|-R)[\s=]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
_ISSUE_NUMBER_RE = re.compile(r"(?:^|[\s#])(\d{1,7})\b")
_ISSUE_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{1,7})\b")

Derive = Callable[[dict, "dict | None"], "tuple[dict | None, dict | None]"]


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
    match = _ISSUE_NUMBER_RE.search(text or "")
    return (int(match.group(1)) if match else None), None


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


def comment_action_from_command(command: str, now: float) -> dict | None:
    """A ``gh issue comment N`` in a shell command is an acknowledgement worth logging."""
    match = _GH_COMMENT_RE.search(command or "")
    if not match:
        return None
    action = {"ts": now, "action": "comment", "issue": int(match.group(1))}
    repo_match = _GH_REPO_RE.search(command)
    if repo_match:
        action["repo"] = repo_match.group(1)
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


def plan_title(plan: str) -> str:
    for line in plan.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "Untitled plan"


def record_factory(payload: dict, current: dict | None, event: str) -> Callable[..., dict]:
    """A ``state(new_state, reason=None, **extra)`` builder that keeps ``issue``,
    ``repo`` and ``started_at`` stable across events and stamps the runtime's
    session id and the event that produced the record."""
    now = time.time()
    current = current or {}
    session_id = payload.get("session_id") or current.get("session_id")

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
        if current.get("last_message") is not None:
            record["last_message"] = current["last_message"]
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


def append_action(state_dir: Path, agent: str, action: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "actions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**action, "session": agent}) + "\n")


def read_current(state_dir: Path, agent: str) -> dict | None:
    try:
        return json.loads((state_dir / f"{agent}.json").read_text())
    except (OSError, ValueError):
        return None


def run_hook(derive: Derive, argv: list[str] | None = None) -> int:
    """Read the CLI's JSON payload from stdin, derive the state, write it.

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
        if action is not None:
            append_action(state_dir, agent, action)
    except OSError:
        return 0
    return 0
