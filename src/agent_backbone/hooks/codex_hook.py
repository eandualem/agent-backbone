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

REFUSAL_SCREEN_LINES = 2000
"""How far back the session's screen is read for the automatic reviewer's refusals."""
REFUSALS_COMPARED = 50
"""Refusals remembered between two looks at the screen."""
WATCH_DIR = "refusal-watch"
"""``<state_dir>/refusal-watch/<agent>.json``: the refusals on screen at the last look,
while a turn that requested an approval goes on; its own file, updated under a lock,
since Codex runs the hooks of parallel tool calls at once."""

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
        "PermissionRequest",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "Interrupt",
        "UserPromptSubmit",
        "SessionEnd",
    }
)
"""Events at which a watching hook reads its screen again."""
_WATCH_EVENTS = frozenset({"PermissionRequest", "PreToolUse", "PostToolUse"})
"""Events within the turn: the watch goes on. After any other look, it ends."""


def own_screen() -> str | None:
    """The recent text of this session's pane, or None outside tmux."""
    pane = os.environ.get("TMUX_PANE", "").strip()
    if not pane or not os.environ.get("TMUX"):
        return None
    try:
        out = bb.subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane, "-S", f"-{REFUSAL_SCREEN_LINES}"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, bb.subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


def screen_refusals(screen: str) -> list[tuple[str, str]]:
    """``(entry, risk)`` for each of the reviewer's refusals on screen, oldest first.

    An entry is the refusal's whole text with its wrapping undone, so a
    re-wrapped screen shows the same entries."""
    found: list[tuple[str, str]] = []
    risk, entry = "", None
    for line in [*screen.splitlines(), ""]:
        if entry is not None:
            if line.startswith("  ") and line.strip():
                entry.append(line)
                continue
            found.append((" ".join(" ".join(entry).split()), risk))
            risk, entry = "", None
        if warned := _RISK.match(line):
            risk = warned.group(1)
        elif _REFUSAL.match(line):
            entry = [line]
        elif line[:1].strip():
            risk = ""  # another entry at the margin: the warning was not this refusal's
    return found


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


def refusal_record(entry: str, risk: str, now: float, ref: str) -> dict:
    """The action-log record of one refusal (``action: permission_denied``)."""
    match = _REFUSAL.match(entry)
    timed_out = bool(match and match.group(1))
    return {
        "ts": now,
        "action": "permission_denied",
        "kind": "auto_review",
        "category": "review timed out" if timed_out else f"risk: {risk}" if risk else "",
        "summary": refusal_summary(match.group(2).strip() if match else ""),
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


def watch_refusals(payload: dict, state_dir: Path, agent: str) -> None:
    """Log the reviewer's refusals that appear once the turn requested an approval.

    ``PermissionRequest`` starts a watch: the refusals already on screen. Each
    later hook of the turn reads the screen again and logs what appeared
    since; the turn's end ends the watch. A screen that cannot be read leaves
    the watch for the next look. A refusal is only read, never answered."""
    event = payload.get("hook_event_name", "")
    directory = state_dir / WATCH_DIR
    target = directory / f"{agent}.json"
    if event == "SessionStart":
        target.unlink(missing_ok=True)  # a new session: nothing to compare with
        return
    if event not in _LOOK_EVENTS or (event != "PermissionRequest" and not target.exists()):
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{agent}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            watch = json.loads(target.read_text())
        except (OSError, ValueError):
            watch = None
        watch = watch if isinstance(watch, list) else None
        if watch is None and event != "PermissionRequest":
            return
        screen = own_screen()
        if screen is None:
            return
        found = screen_refusals(screen)[-REFUSALS_COMPARED:]
        seen = [hashlib.sha256(entry.encode()).hexdigest()[:12] for entry, _ in found]
        if watch is not None:
            now = bb.time.time()
            fresh = found[len(found) - _new_count(watch, seen) :]
            for index, (entry, risk) in enumerate(fresh):
                record = refusal_record(entry, risk, now, f"auto-review:{now:.6f}:{index}")
                bb.append_action(state_dir, agent, record)
        if event in _WATCH_EVENTS:
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(seen))
            os.replace(tmp, target)
        else:
            target.unlink(missing_ok=True)


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
