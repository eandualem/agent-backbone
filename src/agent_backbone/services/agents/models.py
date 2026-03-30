"""State data models — agent state enum and state snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AgentState(StrEnum):
    """Possible agent states."""

    IDLE = "idle"
    STARTING = "starting"
    BUSY = "busy"
    PLAN_WAITING = "plan_waiting"
    PERMISSION_WAITING = "permission_waiting"
    SUB_AGENT_WAITING = "sub_agent_waiting"
    OFFLINE = "offline"


@dataclass
class StateSnapshot:
    """A point-in-time agent state reading."""

    state: AgentState
    current_issue: int | None = None
    timestamp: float = 0.0
    source: str = "default"
    started_at: float | None = None
    plan_file: str | None = None
    plan_title: str | None = None
