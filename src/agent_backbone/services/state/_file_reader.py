"""Push-based state file reading."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_backbone.services.state.models import AgentState, StateSnapshot

log = logging.getLogger(__name__)


def read_state_file(state_dir: Path, session: str) -> StateSnapshot | None:
    """Read push-based state from ~/.claude/state/{session}.json.

    Expected JSON shape:
        {"state": "idle|processing_issue|busy", "issue": 42, "ts": 1234567890.0}

    Returns None if file doesn't exist or is unparseable.
    """
    state_file = state_dir / f"{session}.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
        state_str = data.get("state", "unknown")
        try:
            state = AgentState(state_str)
        except ValueError:
            state = AgentState.UNKNOWN
        started_at_raw = data.get("started_at")
        return StateSnapshot(
            state=state,
            current_issue=data.get("issue"),
            timestamp=float(data.get("ts", 0)),
            source="push",
            started_at=float(started_at_raw) if started_at_raw is not None else None,
            plan_file=data.get("plan_file"),
            plan_title=data.get("plan_title"),
        )
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read state file for %s: %s", session, e)
        return None
