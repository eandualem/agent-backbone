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
        # Outgoing GitHub actions are logged twice: here, *before* the
        # command runs, because the webhook for a `gh pr create` can arrive
        # before a compound command finishes and the backbone would announce
        # the agent's own pull request back to it; and again after it ran
        # (below), with the branch as it actually was. A duplicate entry is
        # harmless; a missing one is not.
        return None, bb.tool_actions(tool, payload.get("tool_input") or {}, payload.get("cwd"), now)
    if event == "PostToolUse":
        tool = payload.get("tool_name", "")
        if tool in ("ExitPlanMode", "AskUserQuestion"):
            return state(STATE_BUSY), None
        return None, bb.tool_actions(tool, payload.get("tool_input") or {}, payload.get("cwd"), now)
    return None, None


def main(argv: list[str] | None = None) -> int:
    return bb.run_hook(derive, argv)


if __name__ == "__main__":
    sys.exit(main())
