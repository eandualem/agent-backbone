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

import os
import sys

try:
    from agent_backbone.hooks import backbone_state as bb
except ImportError:  # copied next to backbone_state.py, outside the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backbone_state as bb  # type: ignore[no-redef]


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
        # A tool runs: any permission dialog is behind us.
        return state(bb.STATE_BUSY), None
    if event == "PostToolUse":
        tool = payload.get("tool_name", "") or ""
        tool_input = payload.get("tool_input") or {}
        command = tool_input.get("command", "")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        action = bb.comment_action_from_command(command or "", now) or bb.comment_action_from_mcp(
            tool, tool_input, now
        )
        return None, action
    if event in ("Stop", "Interrupt"):
        return state(
            bb.STATE_IDLE, last_message=bb.clip_message(payload.get("last_assistant_message"))
        ), None
    return None, None


def main(argv: list[str] | None = None) -> int:
    return bb.run_hook(derive, argv)


if __name__ == "__main__":
    sys.exit(main())
