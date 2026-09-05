#!/usr/bin/env python3
"""Gemini CLI hook: push agent state to the agent-backbone state directory.

Wired at launch through a backbone-owned system-settings file
(``GEMINI_CLI_SYSTEM_SETTINGS_PATH``), so nothing in ``~/.gemini`` or the
repository is touched. Verified against Gemini CLI 0.46: ``SessionStart``
and ``SessionEnd`` arrive with ``session_id``, ``source`` and ``reason``;
``BeforeAgent`` / ``AfterAgent`` bracket a turn and ``Notification``
carries ``notification_type: "ToolPermission"``.

Standard library only — it must run under any ``python3``.
"""

from __future__ import annotations

import os
import re
import sys

try:
    from agent_backbone.hooks import backbone_state as bb
except ImportError:  # copied next to backbone_state.py, outside the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backbone_state as bb  # type: ignore[no-redef]


def tool_succeeded(payload: dict) -> bool:
    """Gemini 0.46 omits a zero exit code but marks failures and background handles."""
    response = payload.get("tool_response")
    if not isinstance(response, dict) or response.get("error"):
        return False
    if bb.response_succeeded(response):
        return True
    data = response.get("data") or {}
    if not isinstance(data, dict) or data.get("isError") or data.get("pid"):
        return False
    content = response.get("llmContent")
    display = response.get("display") or {}
    return (
        isinstance(content, str)
        and content.startswith("Output: ")
        and isinstance(display, dict)
        and display.get("name") == "Shell"
        and not re.search(r"(?m)^(?:Error|Exit Code|Signal|Background PIDs):", content)
    )


def derive(payload: dict, current: dict | None) -> tuple[dict | None, dict | None]:
    """Map a Gemini CLI hook payload to (new_state_record, action_record)."""
    event = payload.get("hook_event_name", "")
    current = current or {}
    state = bb.record_factory(payload, current, event)
    now = bb.time.time()

    if event == "SessionStart":
        return state(bb.STATE_IDLE, started_at=now), None
    if event == "SessionEnd":
        return state(bb.STATE_UNKNOWN), None
    if event == "BeforeAgent":
        issue, repo = bb.issue_from_prompt(payload.get("prompt", "") or "", current)
        return state(bb.STATE_BUSY, issue=issue, repo=repo), None
    if event == "AfterAgent":
        return state(
            bb.STATE_IDLE, last_message=bb.clip_message(payload.get("prompt_response"))
        ), None
    if event == "Notification":
        if (payload.get("notification_type") or "") == "ToolPermission":
            return state(bb.STATE_WAITING, bb.REASON_PERMISSION), None
        return None, None
    if event == "BeforeTool":
        # A tool runs: any permission dialog is behind us.
        return state(bb.STATE_BUSY), None
    if event == "AfterTool":
        return None, (
            bb.action_records(payload, now, phase="succeeded") if tool_succeeded(payload) else []
        )
    return None, None


def main(argv: list[str] | None = None) -> int:
    return bb.run_hook(derive, argv)


if __name__ == "__main__":
    sys.exit(main())
