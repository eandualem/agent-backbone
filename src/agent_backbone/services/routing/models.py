"""Routing data models — dispatch results, session intelligence, and profile."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from agent_backbone.services.agents.models import AgentState


@dataclass
class DispatchResult:
    """Outcome of a dispatch operation."""

    delivered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    offline: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)


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
    runtime: str = "unknown"
    agent_state: AgentState = AgentState.UNKNOWN
    current_issue: int | None = None
    tmux_vars: dict[str, str] = field(default_factory=dict)
