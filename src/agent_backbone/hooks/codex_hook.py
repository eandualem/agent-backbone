#!/usr/bin/env python3
"""Codex CLI hook: push agent state to the agent-backbone state directory.

Wired at launch with ``-c hooks.<Event>=…`` overrides (and
``--dangerously-bypass-hook-trust``, since Codex asks a person to trust
every hook it did not see before; these are the backbone's own). Verified
against codex-cli 0.152: ``SessionStart``, ``UserPromptSubmit`` and
``Stop`` arrive with ``session_id``, ``turn_id`` and, on ``Stop``,
``last_assistant_message``.

Standard library only — it must run under any ``python3``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
from pathlib import Path

try:
    from agent_backbone.hooks import backbone_state as bb
except ImportError:  # copied next to backbone_state.py, outside the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backbone_state as bb  # type: ignore[no-redef]

WATCH_DIR = "refusal-watch"
"""``<state_dir>/refusal-watch/<agent>.json``: the refusals on screen at the last look,
and whether a turn that requested an approval is going on. Its own file, updated
under a lock, since Codex runs the hooks of parallel tool calls at once."""
LOCK_WAIT_SECONDS = 5.0
"""How long a hook waits for the watch file (Codex gives a hook 10 s). The lock
covers reading and writing that file only, never the pane read, so a wait this
long means the machine has stalled; the hook then leaves the watch as it is."""

# Codex's automatic reviewer (``--approve-for-me``) refuses an action without a
# dialog. Codex (0.157) has no hook event for it and leaves it out of the
# session's rollout; its TUI prints it at the left margin, after a warning
# with the risk level (codex-rs/tui/src/history_cell/approvals.rs):
#   ⚠ Automatic approval review denied (risk: high): <the reviewer's rationale>
#   ✗ Request denied for codex to run curl -sS -X POST …
#     <the rest of the command, indented>
# A person's own answer reads "✗ You did not approve …" and is not one.
_REFUSAL = re.compile(
    r"✗ (?:Request denied(?: for codex to)?|(Review timed out) before codex could)(.*)"
)
_RISK = re.compile(r"⚠ Automatic approval review denied \(risk: ([a-z]{1,12})\)")
_NAME = re.compile(r"[A-Za-z0-9_-]{1,60}")
_LOOK_EVENTS = frozenset(
    {
        "SessionStart",
        "PermissionRequest",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "Interrupt",
        "UserPromptSubmit",
        "SessionEnd",
    }
)
"""Events at which the hook may read its screen: at the session's start, and while watching."""
_WATCH_EVENTS = frozenset({"PermissionRequest", "PreToolUse", "PostToolUse"})
"""Events within the turn: the watch goes on. After a look at any other event, it ends."""


def own_screen() -> str | None:
    """All that tmux still holds of this session's pane, wrapped lines joined;
    None when it cannot be read."""
    pane = os.environ.get("TMUX_PANE", "").strip()
    if not pane or not os.environ.get("TMUX"):
        return None
    try:
        out = bb.subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-t", pane, "-S", "-"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, bb.subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


def _entries(screen: str):
    """Each entry at the left margin with its indented continuation, wrapping undone."""
    entry: list[str] | None = None
    for line in [*screen.splitlines(), ""]:
        if entry is not None and line.startswith("  ") and line.strip():
            entry.append(line)
            continue
        if entry is not None:
            yield " ".join(" ".join(entry).split())
            entry = None
        if line[:1].strip():
            entry = [line]


def screen_refusals(screen: str) -> list[tuple[str, str]]:
    """``(entry, risk)`` for each of the reviewer's refusals on screen, oldest first."""
    found: list[tuple[str, str]] = []
    risk = ""
    for entry in _entries(screen):
        if warned := _RISK.match(entry):
            risk = warned.group(1)
            continue
        if _REFUSAL.match(entry):
            found.append((entry, risk))
        risk = ""  # a warning belongs to the refusal right after it
    return found


def _identity(entry: str) -> str:
    """The same refusal however it was wrapped: its text without any whitespace."""
    return hashlib.sha256("".join(entry.split()).encode()).hexdigest()[:12]


def refusal_summary(what: str) -> str:
    """What was refused, named as a Claude refusal is: programs, never arguments."""
    if what.startswith("run "):
        return bb.command_summary(what[4:])
    if what.startswith("apply a patch"):
        return "apply_patch"
    if what.startswith("call MCP tool "):
        server, _, tool = what[len("call MCP tool ") :].strip().partition(".")
        named = _NAME.fullmatch(server) and _NAME.fullmatch(tool)
        return f"{server}: {tool}" if named else "an MCP tool"
    if what.startswith("access "):
        return "network access"
    if what.startswith("send input to terminal"):
        return "input to a running command"
    if what.startswith("request permissions"):
        return "a permission request"
    return "an action"


def _shown(command: str) -> str:
    """A command as the refusal on screen shows it: the first line, cut after
    77 characters with "..." (codex-rs/tui/src/history_cell/approvals.rs)."""
    first, more, _ = command.partition("\n")
    shown = f"{first} ..." if more else first
    return shown if len(shown) <= 80 else shown[:77] + "..."


def _command_key(shown: str) -> str:
    """That text however it was wrapped (whitespace and the trailing dots dropped), hashed."""
    return hashlib.sha256("".join(shown.split()).rstrip(".").encode()).hexdigest()[:12]


def refusal_record(
    entry: str, risk: str, now: float, ref: str, requested: dict[str, str] | None = None
) -> dict:
    """The action-log record of one refusal (``action: permission_denied``).

    A refused command is named from its approval request (``requested``: the
    summaries of the turn's requests by ``_command_key``) when one matches,
    since the screen shows only the command's start."""
    match = _REFUSAL.match(entry)
    timed_out = bool(match and match.group(1))
    what = match.group(2).strip() if match else ""
    # An empty summary marks requests the screen cannot tell apart: the screen names it.
    summary = (requested or {}).get(_command_key(what[4:])) if what.startswith("run ") else None
    return {
        "ts": now,
        "action": "permission_denied",
        "runtime": "codex",
        "kind": "auto_review",
        "category": "review timed out" if timed_out else f"risk: {risk}" if risk else "",
        "summary": summary or refusal_summary(what),
        "tool_use_id": ref,  # no call id reaches a hook; unique per refusal
    }


def _new_count(seen: list[str], now: list[str]) -> int:
    """How many entries at the end of ``now`` were not on screen at the last look.

    The longest run at the end of ``seen`` that starts ``now`` is still on
    screen; what came before it scrolled away, and what follows it is new."""
    for kept in range(min(len(seen), len(now)), 0, -1):
        if seen[-kept:] == now[:kept]:
            return len(now) - kept
    return len(now)


@contextlib.contextmanager
def _watch_file(target: Path):
    """The watch, read and then written back under the agent's lock; None when
    the lock is not had in time, and nothing is written then."""
    with target.with_suffix(".lock").open("a") as lock:
        deadline = bb.time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if bb.time.monotonic() > deadline:
                    yield None
                    return
                bb.time.sleep(0.02)
        try:
            saved = json.loads(target.read_text())
        except (OSError, ValueError):
            saved = {}
        saved = saved if isinstance(saved, dict) else {}
        watch = {
            "seen": saved.get("seen") if isinstance(saved.get("seen"), list) else None,
            "watching": saved.get("watching") is True,
            "requested": saved.get("requested") if isinstance(saved.get("requested"), dict) else {},
            "read_at": saved.get("read_at") if isinstance(saved.get("read_at"), float) else 0.0,
        }
        yield watch
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(watch))
        os.replace(tmp, target)


def watch_refusals(payload: dict, state_dir: Path, agent: str) -> None:
    """Log the reviewer's refusals that appear once a turn requested an approval.

    The session's start records the refusals already on screen. A
    ``PermissionRequest`` starts a watch; every later hook of the turn reads
    the screen again and logs what appeared since the last reading, and a
    look at the turn's end ends the watch. The event is recorded first, the
    screen read without the lock, and the reading applied only when no later
    one was: hooks of parallel calls never wait on another's screen read. A
    screen that cannot be read leaves everything for the next look. A
    refusal is only read, never answered."""
    event = payload.get("hook_event_name", "")
    if event not in _LOOK_EVENTS or not os.environ.get("TMUX_PANE", "").strip():
        return
    directory = state_dir / WATCH_DIR
    target = directory / f"{agent}.json"
    if event not in ("SessionStart", "PermissionRequest") and not target.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    with _watch_file(target) as watch:
        if watch is None:
            return
        if event == "SessionStart":  # a new session: what was seen before means nothing
            watch.update(seen=None, watching=False, requested={}, read_at=0.0)
        elif event == "PermissionRequest":
            watch["watching"] = True
            tool_input = payload.get("tool_input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if isinstance(command, str):
                key = _command_key(_shown(command))
                summary = bb.action_summary(str(payload.get("tool_name") or ""), tool_input)
                if watch["requested"].get(key, summary) != summary:
                    summary = ""  # two requests the screen shows alike: neither names it
                watch["requested"][key] = summary  # every request of the turn, until it ends
        elif not watch["watching"]:
            return
    started = bb.time.time()
    screen = own_screen()
    if screen is None:
        return
    found = screen_refusals(screen)  # every one tmux still holds
    now_seen = [_identity(entry) for entry, _ in found]
    with _watch_file(target) as watch:
        if watch is None or started <= watch["read_at"]:
            return  # a later reading is already in: this one is older
        if watch["seen"] is not None:  # never guess what an unknown earlier screen held
            now = bb.time.time()
            fresh = found[len(found) - _new_count(watch["seen"], now_seen) :]
            for index, (entry, risk) in enumerate(fresh):
                ref = f"auto-review:{now:.6f}:{index}"
                record = refusal_record(entry, risk, now, ref, watch["requested"])
                bb.append_action(state_dir, agent, record)
        watch.update(seen=now_seen, read_at=started)
        if event not in _WATCH_EVENTS:  # a look at the turn's end ends the watch
            watch.update(watching=False, requested={})


def tool_succeeded(payload: dict) -> bool:
    """Codex emits PostToolUse for failed Bash commands too; inspect its result."""
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            match = re.match(
                r"\A(?:Chunk ID: [^\n]+\n)?Wall time: [^\n]+\n"
                r"(?:Process exited with code|Exit code:) (\d+)\n(?:Final output|Output):",
                response,
            )
            return bool(match and match.group(1) == "0")
    if isinstance(response, dict):
        if any(response.get(key) for key in ("isError", "is_error", "error", "interrupted")):
            return False
        if isinstance(response.get("metadata"), dict):
            return bb.response_succeeded(response["metadata"])
        if (payload.get("tool_name") or "").startswith("mcp__"):
            return isinstance(response.get("content"), list)
    return bb.response_succeeded(response)


def derive(payload: dict, current: dict | None) -> tuple[dict | None, dict | None]:
    """Map a Codex hook payload to (new_state_record, action_record)."""
    event = payload.get("hook_event_name", "")
    current = current or {}
    state = bb.record_factory(payload, current, event)
    now = bb.time.time()

    if event == "SessionStart":
        return state(bb.STATE_IDLE, started_at=now), None
    if event == "SessionEnd":
        return state(bb.STATE_UNKNOWN), None
    if event == "UserPromptSubmit":
        issue, repo = bb.issue_from_prompt(payload.get("prompt", "") or "", current)
        return state(bb.STATE_BUSY, issue=issue, repo=repo), None
    if event == "PermissionRequest":
        return state(bb.STATE_WAITING, bb.REASON_PERMISSION), None
    if event == "PreToolUse":
        # Intent suppresses a fast self-event, but does not acknowledge work.
        actions = bb.action_records(payload, now, phase="intent")
        return state(bb.STATE_BUSY), actions or None
    if event == "PostToolUse":
        actions = (
            bb.action_records(payload, now, phase="succeeded") if tool_succeeded(payload) else []
        )
        return None, actions
    if event in ("Stop", "Interrupt"):
        return state(
            bb.STATE_IDLE, last_message=bb.clip_message(payload.get("last_assistant_message"))
        ), None
    return None, None


CONTEXT_EVENTS = frozenset({"PostToolUse", "SessionStart"})
"""Events whose JSON output adds context to the model (``hookSpecificOutput.additionalContext``)."""


def main(argv: list[str] | None = None) -> int:
    return bb.run_hook(
        derive,
        argv,
        context_events=CONTEXT_EVENTS,
        turn_end_events=frozenset({"Stop", "Interrupt"}),
        observe=watch_refusals,
    )


if __name__ == "__main__":
    sys.exit(main())
