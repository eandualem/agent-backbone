"""Delivery data models — session intelligence and profile."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from agent_backbone.services.state import AgentState


class SessionIntelligence(StrEnum):
    """Composite session state derived from tmux vars + agent state."""

    IDLE_READY = "idle_ready"
    IDLE_GRACE = "idle_grace"
    COPY_MODE = "copy_mode"
    USER_INTERACTING = "user_interacting"
    AGENT_WORKING = "agent_working"
    PLAN_WAITING = "plan_waiting"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class SessionProfile:
    """Point-in-time composite session state."""

    session_name: str
    intelligence: SessionIntelligence
    agent_state: AgentState = AgentState.UNKNOWN
    tmux_vars: dict[str, str] = field(default_factory=dict)
