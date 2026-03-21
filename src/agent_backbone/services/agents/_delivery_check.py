"""Delivery decision logic — outgoing comment detection and should_deliver."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_backbone.services.agents.models import AgentState


def find_outgoing_comment(
    issue_number: int,
    repo_full_name: str | None = None,
    action_log: str = "~/.claude/state/github-actions.jsonl",
    max_lines: int = 50,
    recency_seconds: float = 30.0,
) -> str | None:
    """Check if a comment on this issue was recently made by one of our agents.

    Reads the tail of the JSONL action log written by PostToolUse hooks.
    Matches by issue number + recency window (no comment_id in the log).
    Returns the originating session name, or None if not found.
    Graceful: returns None if the log file doesn't exist (hooks not yet set up).

    Log format: {"ts": 1234567890.0, "session": "ike", "action": "comment", "issue": 42}
    """
    log_path = Path(action_log).expanduser()
    if not log_path.exists():
        return None
    try:
        lines = log_path.read_text().strip().splitlines()
        now = time.time()
        # Only check the tail, most recent first
        for line in reversed(lines[-max_lines:]):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry_repo = entry.get("repo_full_name") or entry.get("repo") or ""
            if (
                entry.get("action") == "comment"
                and entry.get("issue") == issue_number
                and (not repo_full_name or entry_repo == repo_full_name)
            ):
                ts = entry.get("ts", 0)
                if now - ts <= recency_seconds:
                    return entry.get("session")
        return None
    except OSError:
        return None


def has_commented_on_issue(
    issue_number: int,
    session: str,
    repo_full_name: str | None = None,
    action_log: str = "~/.claude/state/github-actions.jsonl",
    max_lines: int = 200,
) -> bool:
    """Check if a session has ever commented on this issue (per action log).

    Unlike find_outgoing_comment(), this has NO recency window.
    Any comment at any time counts as acknowledgment.
    """
    log_path = Path(action_log).expanduser()
    if not log_path.exists():
        return False
    try:
        lines = log_path.read_text().strip().splitlines()
        for line in reversed(lines[-max_lines:]):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                entry.get("action") == "comment"
                and entry.get("issue") == issue_number
                and entry.get("session") == session
                and (
                    not repo_full_name
                    or (entry.get("repo_full_name") or entry.get("repo") or "") == repo_full_name
                )
            ):
                return True
        return False
    except OSError:
        return False


def should_deliver(
    state: AgentState,
    is_blocking: bool = False,
    busy_duration: float | None = None,
    busy_threshold: float = 1800.0,
    require_idle: bool = False,
) -> bool:
    """Decide whether to deliver a notification based on agent state.

    Delivery gating is strict in all modes: only confirmed idle agents
    should receive a new issue. Extra parameters are retained for API
    compatibility with older call sites and tests.
    """
    del is_blocking, busy_duration, busy_threshold, require_idle
    return state == AgentState.IDLE
