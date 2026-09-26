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

# Claude Code hands a pasted prompt to the hook inside ``<pasted_content id=…>`` tags.
_PASTE_WRAPPER = re.compile(r"\s*<pasted_content\b([^>]*)>(.*)</pasted_content\1>\s*", re.DOTALL)


def _unwrapped(payload: dict) -> dict:
    """The payload with the paste wrapper around its whole prompt removed, so
    the prompt's digest is that of the text delivery pasted. Tags inside the
    pasted text are part of it and stay."""
    prompt = payload.get("prompt")
    wrapped = _PASTE_WRAPPER.fullmatch(prompt) if isinstance(prompt, str) else None
    return {**payload, "prompt": wrapped.group(2)} if wrapped else payload


def derive(payload: dict, current: dict | None) -> tuple[dict | None, dict | None]:
    """Map a Claude Code hook payload to (new_state_record, action_record)."""
    event = payload.get("hook_event_name", "")
    current = current or {}
    state = bb.record_factory(_unwrapped(payload), current, event)
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


CHROME_GROUPS_DIR = "chrome-groups"
"""``<state_dir>/chrome-groups/<agent>.<group>.json``: a Chrome tab group this
agent's Claude-in-Chrome sessions used, read by the optional tab-names extension."""
CHROME_GROUP_MAX_AGE = 7 * 24 * 3600
"""A record this old is from a long-closed group; the agent's hook removes it."""
_CHROME_CONTEXT_TOOL = "mcp__claude-in-chrome__tabs_context_mcp"
"""The one tool whose result is the extension's own account of the session's
group; other tools (javascript_tool) can return page-controlled JSON."""


def _tab_context(response) -> dict | None:
    """The tab context a Claude-in-Chrome result carries as JSON in a text block.

    Only the decoded object's own fields count: tab titles are page-controlled
    and may contain anything, including text that looks like an id."""
    blocks = response.get("content") if isinstance(response, dict) else response
    for block in blocks if isinstance(blocks, list) else []:
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str) or not text.lstrip().startswith("{"):
            continue
        try:
            context = json.loads(text)
        except ValueError:
            continue
        if isinstance(context, dict) and isinstance(context.get("tabGroupId"), int):
            return context
    return None


def record_chrome_group(payload: dict, state_dir: Path, agent: str) -> None:
    """Remember the tab group and tabs a Claude-in-Chrome result names.

    Each session's group is titled "Claude"; this record is what lets the
    optional extension name it after the agent. One file per agent and
    group: an earlier group still open keeps its record, and hooks of
    different agents never write the same file."""
    if payload.get("hook_event_name") != "PostToolUse":
        return
    if payload.get("tool_name") != _CHROME_CONTEXT_TOOL:
        return
    context = _tab_context(payload.get("tool_response"))
    if context is None:
        return
    tabs = context.get("availableTabs")
    record = {
        "agent": agent,
        "group": context["tabGroupId"],
        "tabs": sorted(
            {
                tab["tabId"]
                for tab in (tabs if isinstance(tabs, list) else [])
                if isinstance(tab, dict) and isinstance(tab.get("tabId"), int)
            }
        ),
        "ts": bb.time.time(),
    }
    directory = state_dir / CHROME_GROUPS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{agent}.{record['group']}.json"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, target)
    for old in directory.glob(f"{agent}.*.json"):
        if record["ts"] - old.stat().st_mtime > CHROME_GROUP_MAX_AGE:
            old.unlink(missing_ok=True)


_CATEGORY = re.compile(r"\[([^\]\n]{1,80})\]")


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
        "runtime": "claude",
        "kind": "classifier",
        "category": category,
        "tool": str(payload.get("tool_name") or ""),
        "summary": bb.action_summary(
            str(payload.get("tool_name") or ""), payload.get("tool_input")
        ),
        "tool_use_id": str(payload.get("tool_use_id") or ""),
    }


CONTEXT_EVENTS = frozenset({"PostToolUse", "SessionStart"})
"""Events whose JSON output adds context to the model (``hookSpecificOutput.additionalContext``)."""


def main(argv: list[str] | None = None) -> int:
    return bb.run_hook(
        derive,
        argv,
        context_events=CONTEXT_EVENTS,
        turn_end_events=frozenset({"Stop"}),
        observe=record_chrome_group,
    )


if __name__ == "__main__":
    sys.exit(main())
