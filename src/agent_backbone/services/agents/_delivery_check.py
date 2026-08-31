"""Delivery decision helpers — action-log lookups and the idle check."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_backbone.services.agents.models import AgentState


def _read_tail(action_log: str | Path | None, max_lines: int) -> list[dict]:
    if action_log is None:
        return []
    log_path = Path(action_log).expanduser()
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text().strip().splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in reversed(lines[-max_lines:]):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _repo_matches(entry: dict, repo: str) -> bool:
    entry_repo = entry.get("repo") or ""
    return not entry_repo or not repo or entry_repo.casefold() == repo.casefold()


def find_outgoing_comment(
    issue_number: int,
    action_log: str | Path | None = None,
    max_lines: int = 50,
    recency_seconds: float = 30.0,
    *,
    repo: str = "",
) -> str | None:
    """Session that recently commented on an issue according to the hook action log.

    Log format: ``{"ts": 1234567890.0, "session": "reviewer", "action": "comment",
    "issue": 42, "repo": "owner/name"}``.
    """
    now = time.time()
    for entry in _read_tail(action_log, max_lines):
        if entry.get("action") != "comment" or entry.get("issue") != issue_number:
            continue
        if not _repo_matches(entry, repo):
            continue
        if now - float(entry.get("ts", 0)) <= recency_seconds:
            return entry.get("session")
    return None


def has_commented_on_issue(
    issue_number: int,
    session: str,
    action_log: str | Path | None = None,
    max_lines: int = 200,
    *,
    repo: str = "",
) -> bool:
    """Whether a session has ever commented on this issue (per action log)."""
    for entry in _read_tail(action_log, max_lines):
        if (
            entry.get("action") == "comment"
            and entry.get("issue") == issue_number
            and entry.get("session") == session
            and _repo_matches(entry, repo)
        ):
            return True
    return False


def should_deliver(state: AgentState, **_ignored) -> bool:
    """Only a confirmed idle agent should receive a new issue."""
    return state == AgentState.IDLE
