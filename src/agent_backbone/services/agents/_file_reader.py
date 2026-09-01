"""Push-based state file reading (written by runtime hooks)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_backbone.services.agents.models import AgentState, StateSnapshot

log = logging.getLogger(__name__)


def read_state_file(state_dir: Path, session: str) -> StateSnapshot | None:
    """Read hook-written state from ``<state_dir>/<session>.json``.

    Expected JSON shape::

        {"state": "idle|busy|waiting_for_human|starting|unknown", "reason": "plan",
         "issue": 42, "repo": "owner/name", "ts": 1234567890.0,
         "started_at": 1234567800.0, "plan_file": "...", "plan_title": "..."}

    Returns None if the file does not exist or cannot be parsed.
    """
    state_file = state_dir / f"{session}.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read state file for %s: %s", session, e)
        return None

    state = AgentState.parse(data.get("state"))
    started_at_raw = data.get("started_at")
    return StateSnapshot(
        state=state,
        reason=data.get("reason") or None,
        current_issue=data.get("issue"),
        current_repo=data.get("repo") or None,
        timestamp=float(data.get("ts", 0)),
        source="push",
        started_at=float(started_at_raw) if started_at_raw is not None else None,
        plan_file=data.get("plan_file"),
        plan_title=data.get("plan_title"),
        evidence=[f"hook state file {state_file.name}: {state.value}"],
    )
