#!/usr/bin/env python3
"""Claude Code hook: push agent state to the agent-backbone state directory.

Wired at launch through the backbone-owned ``--settings`` file (or
installed into ``~/.claude/settings.json`` by ``backbone hooks install
claude``). Claude Code invokes this script for the configured events with
a JSON payload on stdin; the shared ``backbone_state`` module writes
``<state_dir>/<agent>.json`` and the action log.

Standard library only — it must run under any ``python3``.
"""

from __future__ import annotations

import os
import re
import shlex
import sys

try:
    from agent_backbone.hooks import backbone_state as bb
except ImportError:  # copied next to backbone_state.py, outside the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backbone_state as bb  # type: ignore[no-redef]

STATE_IDLE = bb.STATE_IDLE
STATE_BUSY = bb.STATE_BUSY
STATE_WAITING = bb.STATE_WAITING
STATE_UNKNOWN = bb.STATE_UNKNOWN
STATE_BLOCKED = bb.STATE_BLOCKED
REASON_PLAN = bb.REASON_PLAN
REASON_PERMISSION = bb.REASON_PERMISSION
REASON_QUESTION = bb.REASON_QUESTION
resolve_agent = bb.resolve_agent
subprocess = bb.subprocess  # tests patch the tmux lookup through this name


def derive(payload: dict, current: dict | None) -> tuple[dict | None, dict | None]:
    """Map a Claude Code hook payload to (new_state_record, action_record)."""
    event = payload.get("hook_event_name", "")
    current = current or {}
    state = bb.record_factory(payload, current, event)
    now = bb.time.time()

    if event == "SessionStart":
        # Fired once Claude Code is at its prompt: ready for input.
        return state(STATE_IDLE, started_at=now), None
    if event == "SessionEnd":
        return state(STATE_UNKNOWN), None
    if event == "UserPromptSubmit":
        issue, repo = bb.issue_from_prompt(payload.get("prompt", "") or "", current)
        return state(STATE_BUSY, issue=issue, repo=repo), None
    if event == "Stop":
        return state(
            STATE_IDLE, last_message=bb.clip_message(payload.get("last_assistant_message"))
        ), None
    if event == "Notification":
        kind = (payload.get("notification_type") or "").lower()
        message = payload.get("message", "") or ""
        if kind.startswith("quota_auto_resume"):
            # The usage limit: Claude Code pauses and resumes on its own.
            if kind in ("quota_auto_resume_fired", "quota_auto_resume_stale_resumed"):
                return state(STATE_BUSY), None
            return state(bb.STATE_BLOCKED, bb.REASON_QUOTA, detail=bb.clip_message(message)), None
        lowered = message.lower()
        if "permission" in lowered:
            return state(STATE_WAITING, REASON_PERMISSION), None
        if "waiting for your input" in lowered:
            return state(STATE_IDLE), None
        return None, None
    if event == "PreToolUse":
        tool = payload.get("tool_name", "")
        if tool == "ExitPlanMode":
            plan = (payload.get("tool_input") or {}).get("plan", "") or ""
            return state(
                STATE_WAITING, REASON_PLAN, plan_title=bb.plan_title(plan), plan_text=plan
            ), None
        if tool == "AskUserQuestion":
            return state(STATE_WAITING, REASON_QUESTION), None
        return None, bb.action_records(payload, now, phase="intent")
    if event == "PostToolUse":
        tool = payload.get("tool_name", "")
        if tool in ("ExitPlanMode", "AskUserQuestion"):
            return state(STATE_BUSY), None
        # Claude sends failures to PostToolUseFailure. Still reject explicit
        # failure/interruption and background handles in successful tool output.
        response = payload.get("tool_response")
        if (
            not isinstance(response, dict)
            or not response
            or any(
                response.get(key) for key in ("isError", "error", "interrupted", "backgroundTaskId")
            )
        ):
            return None, []
        for key in ("exit_code", "exitCode"):
            if key in response and response[key] != 0:
                return None, []
        if response.get("success") is False:
            return None, []
        return None, bb.action_records(payload, now, phase="succeeded")
    if event == "PermissionDenied":
        # Auto mode's classifier refused a call, with no dialog (a permission
        # rule's refusal does not fire this event: measured, 2.1.282). The
        # agent keeps working, so no state changes; the backbone tells the
        # humans from this record, and never asks for a retry.
        return None, denial_record(payload, now)
    return None, None


_CATEGORY = re.compile(r"\[([^\]\n]{1,80})\]")
_WORD = re.compile(r"[a-z][a-z0-9_-]{0,30}")


def _command_summary(command: str) -> str:
    """``gh issue edit`` for ``cd x && gh issue edit 40 --body-file …``: each
    segment's program and plain subcommands, never arguments, flags or paths."""
    parts = []
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        words = [w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        if not words:
            continue
        summary = [os.path.basename(words[0])]
        for word in words[1:3]:
            if not _WORD.fullmatch(word):
                break
            summary.append(word)
        if _WORD.fullmatch(summary[0]) and " ".join(summary) not in parts:
            parts.append(" ".join(summary))
    return "; ".join(parts[:3]) or "a shell command"


def _action_summary(tool: str, tool_input) -> str:
    """What was refused, safely: names of programs and actions, not their input."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        return _command_summary(str(tool_input.get("command") or ""))
    match = re.fullmatch(r"mcp__(.+?)__(.+)", tool)
    if not match:
        return tool
    server, name = match.groups()
    steps = []
    for action in tool_input.get("actions") or [tool_input]:
        if not isinstance(action, dict):
            continue
        step = str(action.get("name") or name)
        inner = action.get("input") if isinstance(action.get("input"), dict) else action
        if _WORD.fullmatch(str(inner.get("action") or "")):
            step += f":{inner['action']}"
        if _WORD.fullmatch(step.replace(":", "_")) and step not in steps:
            steps.append(step)
    return f"{server}: {name}" + (f" ({', '.join(steps[:4])})" if steps and steps != [name] else "")


def denial_record(payload: dict, now: float) -> dict:
    """The action-log record of one refused tool call (``action: permission_denied``)."""
    reason = " ".join(str(payload.get("reason") or "").split())
    bracketed = _CATEGORY.search(reason)
    # The classifier's category ("External System Writes"), never its prose,
    # which may describe the action's content.
    category = bracketed.group(1).strip() if bracketed else reason if len(reason) <= 60 else ""
    return {
        "ts": now,
        "action": "permission_denied",
        "kind": "classifier",
        "category": category,
        "tool": str(payload.get("tool_name") or ""),
        "summary": _action_summary(str(payload.get("tool_name") or ""), payload.get("tool_input")),
        "tool_use_id": str(payload.get("tool_use_id") or ""),
    }


CONTEXT_EVENTS = frozenset({"PostToolUse"})
"""Events whose JSON output adds context to the model (``hookSpecificOutput.additionalContext``)."""


def main(argv: list[str] | None = None) -> int:
    return bb.run_hook(
        derive, argv, context_events=CONTEXT_EVENTS, turn_end_events=frozenset({"Stop"})
    )


if __name__ == "__main__":
    sys.exit(main())
