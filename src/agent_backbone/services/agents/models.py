"""State data models — agent state enum and state snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AgentState(StrEnum):
    """Possible agent states."""

    IDLE = "idle"
    STARTING = "starting"
    BUSY = "busy"
    PLAN_WAITING = "plan_waiting"
    PERMISSION_WAITING = "permission_waiting"
    SUB_AGENT_WAITING = "sub_agent_waiting"
    OFFLINE = "offline"


StateContext = dict[str, Any] | str | None


def serialize_state_context(context: StateContext) -> str | None:
    """Serialize structured state context for database persistence."""
    if context is None:
        return None
    if isinstance(context, str):
        return context or None
    if not context:
        return None
    return json.dumps(context, sort_keys=True, ensure_ascii=True)


def deserialize_state_context(raw: str | None) -> StateContext:
    """Best-effort decode of persisted state context."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return raw


@dataclass
class StateSnapshot:
    """A point-in-time agent state reading."""

    state: AgentState
    current_issue: int | None = None
    timestamp: float = 0.0
    source: str = "default"
    started_at: float | None = None
    context: StateContext = None
    plan_file: str | None = None
    plan_title: str | None = None
