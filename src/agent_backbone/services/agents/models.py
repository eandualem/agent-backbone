"""State data models — universal agent states and snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AgentState(StrEnum):
    """What the agent is doing. Runtime-agnostic.

    ``waiting_for_human`` covers plan approval, permission prompts and any
    other question the runtime is blocking on; the detail is in
    ``StateSnapshot.reason`` (``plan``, ``permission``, ``question``).
    """

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    WAITING_FOR_HUMAN = "waiting_for_human"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str | None) -> tuple[AgentState, str | None]:
        """Parse a stored/legacy value into (state, reason)."""
        if not value:
            return cls.UNKNOWN, None
        legacy = {
            "processing_issue": (cls.BUSY, None),
            "plan_waiting": (cls.WAITING_FOR_HUMAN, "plan"),
            "permission_waiting": (cls.WAITING_FOR_HUMAN, "permission"),
        }
        if value in legacy:
            return legacy[value]
        try:
            return cls(value), None
        except ValueError:
            return cls.UNKNOWN, None


WORKING_STATES = frozenset({AgentState.STARTING, AgentState.BUSY})

REASON_PLAN = "plan"
REASON_PERMISSION = "permission"
REASON_QUESTION = "question"


@dataclass
class StateSnapshot:
    """A point-in-time agent state reading with the evidence behind it."""

    state: AgentState
    reason: str | None = None
    current_issue: int | None = None
    current_repo: str | None = None
    timestamp: float = 0.0
    source: str = "default"
    started_at: float | None = None
    plan_file: str | None = None
    plan_title: str | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def is_working(self) -> bool:
        return self.state in WORKING_STATES

    @property
    def is_plan_waiting(self) -> bool:
        return self.state == AgentState.WAITING_FOR_HUMAN and self.reason == REASON_PLAN
