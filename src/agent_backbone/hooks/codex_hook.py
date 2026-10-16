#!/usr/bin/env python3
"""Codex CLI hook: push agent state to the agent-backbone state directory.

Wired at launch with ``-c hooks.<Event>=…`` overrides (and
``--dangerously-bypass-hook-trust``, since Codex asks a person to trust
every hook it did not see before; these are the backbone's own). Verified
against codex-cli 0.152: ``SessionStart``, ``UserPromptSubmit`` and
``Stop`` arrive with ``session_id``, ``turn_id`` and, on ``Stop``,
``last_assistant_message`` and ``transcript_path``.

Standard library only — it must run under any ``python3``.
"""

from __future__ import annotations

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


PROPOSED_PLAN = re.compile(r"<proposed_plan>\s*(.*?)\s*</proposed_plan>", re.DOTALL)


def proposed_plan(payload: dict) -> str | None:
    """The plan this turn proposed: the ``<proposed_plan>`` block of its reply.

    In Plan mode Codex ends a finished plan with that block and then asks
    "Implement this plan?". It takes the block out of the ``Stop`` payload's
    ``last_assistant_message`` (codex-cli 0.157.1), so the reply is read from
    the session's rollout, where it is kept whole. A reply of an earlier turn
    is not this turn's plan.
    """
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    for record in bb.transcript_records(Path(transcript)):
        item = record.get("payload")
        if record.get("type") != "response_item" or not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        meta = item.get("internal_chat_message_metadata_passthrough")
        turn = meta.get("turn_id") if isinstance(meta, dict) else None
        if turn and payload.get("turn_id") and turn != payload["turn_id"]:
            return None
        parts = item.get("content") if isinstance(item.get("content"), list) else []
        text = "".join(
            part.get("text") or ""
            for part in parts
            if isinstance(part, dict) and part.get("type") == "output_text"
        )
        match = PROPOSED_PLAN.search(text)
        return match.group(1) if match and match.group(1) else None
    return None


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
        last_message = bb.clip_message(payload.get("last_assistant_message"))
        if event == "Stop" and (plan := proposed_plan(payload)):
            return state(
                bb.STATE_WAITING,
                bb.REASON_PLAN,
                last_message=last_message,
                plan_title=bb.plan_title(plan),
                plan_text=plan,
            ), None
        return state(bb.STATE_IDLE, last_message=last_message), None
    return None, None


CONTEXT_EVENTS = frozenset({"PostToolUse", "SessionStart"})
"""Events whose JSON output adds context to the model (``hookSpecificOutput.additionalContext``)."""


def main(argv: list[str] | None = None) -> int:
    return bb.run_hook(
        derive,
        argv,
        context_events=CONTEXT_EVENTS,
        turn_end_events=frozenset({"Stop", "Interrupt"}),
    )


if __name__ == "__main__":
    sys.exit(main())
