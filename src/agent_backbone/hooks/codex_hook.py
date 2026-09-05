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

import json
import os
import re
import sys

try:
    from agent_backbone.hooks import backbone_state as bb
except ImportError:  # copied next to backbone_state.py, outside the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backbone_state as bb  # type: ignore[no-redef]


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


def main(argv: list[str] | None = None) -> int:
    return bb.run_hook(derive, argv)


if __name__ == "__main__":
    sys.exit(main())
