"""Routing data models — dispatch results and delivery conditions."""

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
    """Why a session can or cannot receive a message right now.

    Derived from the agent state plus terminal conditions. Copy mode is not
    a value here: it is cleared automatically before the decision is made.
    """

    READY = "ready"
    SETTLING = "settling"
    HUMAN_TYPING = "human_typing"
    AGENT_WORKING = "agent_working"
    WAITING_FOR_HUMAN = "waiting_for_human"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class SessionProfile:
    """Point-in-time delivery readiness of a session, with evidence."""

    session_name: str
    intelligence: SessionIntelligence
    runtime: str = "unknown"
    agent_state: AgentState = AgentState.UNKNOWN
    reason: str | None = None
    current_issue: int | None = None
    current_repo: str | None = None
    state_source: str = "default"
    tmux_vars: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
