"""State data models — universal agent states and snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AgentState(StrEnum):
    """What the agent is doing. Runtime-agnostic.

    ``waiting_for_human`` covers plan approval, permission prompts and any
    other question the runtime is blocking on; the detail is in
    ``StateSnapshot.reason`` (``plan``, ``permission``, ``question``).
    ``blocked`` is the runtime waiting on something that is not a person —
    today its usage limit (``reason`` ``quota``); it resumes on its own.
    """

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    WAITING_FOR_HUMAN = "waiting_for_human"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str | None) -> AgentState:
        """A stored value as a state; anything unrecognised is ``unknown``."""
        try:
            return cls(value or "")
        except ValueError:
            return cls.UNKNOWN


WORKING_STATES = frozenset({AgentState.STARTING, AgentState.BUSY, AgentState.BLOCKED})
"""States in which the agent is occupied and will come back by itself."""

REASON_PLAN = "plan"
REASON_PERMISSION = "permission"
REASON_QUESTION = "question"
REASON_QUOTA = "quota"


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
    session_id: str | None = None
    """The runtime's own session id, when its hook reports one."""
    last_message: str | None = None
    """The agent's last reply (clipped), when its hook reports one."""
    event: str | None = None
    """The hook event that produced a push snapshot."""
    detail: str | None = None
    """What the runtime said about a ``blocked`` state (e.g. when the limit resets)."""
    evidence: list[str] = field(default_factory=list)

    @property
    def is_plan_waiting(self) -> bool:
        return self.state == AgentState.WAITING_FOR_HUMAN and self.reason == REASON_PLAN

    def db_fields(self) -> dict:
        """Keyword arguments for ``BackboneDB.set_agent_state`` — one spelling for every writer."""
        return {
            "state": self.state.value,
            "reason": self.reason,
            "current_issue": self.current_issue,
            "current_repo": self.current_repo,
            "ts": str(self.timestamp) if self.timestamp else None,
            "started_at": str(self.started_at) if self.started_at else None,
            "plan_file": self.plan_file,
            "plan_title": self.plan_title,
        }
