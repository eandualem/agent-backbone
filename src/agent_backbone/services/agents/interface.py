"""Agent state tracking service and monitoring coordination service — LifecycleAware wrappers."""

from __future__ import annotations  # noqa: I001

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.agents._heartbeat import (
    load_schedules as _load_schedules,
    save_schedules as _save_schedules,
)
from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.agents._observation import (
    observe_session,
    snapshot_from_observation,
)

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_LEGACY_STATE_ALIASES = {
    "processing_issue": AgentState.BUSY.value,
    "unknown": AgentState.OFFLINE.value,
}


def _row_to_snapshot(row: dict) -> StateSnapshot:
    """Convert a DB agent_states row dict to a StateSnapshot."""
    state_str = _LEGACY_STATE_ALIASES.get(
        str(row.get("state", AgentState.OFFLINE.value)),
        str(row.get("state", AgentState.OFFLINE.value)),
    )
    try:
        state = AgentState(state_str)
    except ValueError:
        state = AgentState.OFFLINE

    ts_raw = row.get("ts")
    timestamp = float(ts_raw) if ts_raw else 0.0

    started_raw = row.get("started_at")
    started_at = float(started_raw) if started_raw else None

    return StateSnapshot(
        state=state,
        current_issue=row.get("current_issue"),
        timestamp=timestamp,
        source="db",
        started_at=started_at,
        plan_file=row.get("plan_file"),
        plan_title=row.get("plan_title"),
    )


async def _default_snapshot(session: str) -> StateSnapshot:
    """Return the synthesized fallback snapshot for a session with no DB row."""
    observation = await observe_session(session)
    if observation.online:
        return StateSnapshot(state=AgentState.STARTING, source="default")
    return snapshot_from_observation(observation, timestamp=0.0)


def reset_push_timestamps() -> None:
    """Compatibility no-op for legacy test fixtures."""
    return None


class StateService:
    """Agent state tracking service implementing LifecycleAware."""

    def __init__(
        self,
        state_dir: str = "~/.claude/state",
        stale_threshold: int = 300,
        db: BackboneDB | None = None,
    ) -> None:
        del state_dir, stale_threshold
        self._db = db

    async def start(self) -> None:
        log.info("State service started: db=%s", bool(self._db))

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {
            "healthy": self._db is not None,
            "service": "state",
            "db_backed": self._db is not None,
        }

    # --- DI surface for route handlers ---

    async def get_state(self, session: str) -> StateSnapshot:
        """Get the current state for one session from PostgreSQL or live defaults."""
        snapshot = await self.get_reported_state(session)
        if snapshot.state == AgentState.OFFLINE:
            observation = await observe_session(session)
            if observation.online:
                return StateSnapshot(
                    state=AgentState.STARTING,
                    current_issue=snapshot.current_issue,
                    timestamp=snapshot.timestamp,
                    source="default",
                    started_at=snapshot.started_at,
                    plan_file=snapshot.plan_file,
                    plan_title=snapshot.plan_title,
                )
        return snapshot

    async def get_reported_state(self, session: str) -> StateSnapshot:
        """Get the raw reported state for one session from PostgreSQL."""
        if self._db is None:
            return await _default_snapshot(session)

        if self._db is not None:
            try:
                row = await self._db.get_agent_state(session)
                if row is not None:
                    return _row_to_snapshot(row)
            except Exception:
                log.warning("DB state read failed for %s", session)
        return await _default_snapshot(session)

    async def observe_session(self, session: str):
        """Observe a session's tmux/process state directly."""
        return await observe_session(session)

    async def observe_state(self, session: str) -> StateSnapshot:
        """Observe the tmux/process-derived state directly."""
        observation = await observe_session(session)
        return snapshot_from_observation(observation)


class MonitoringService:
    """Monitoring coordination service implementing LifecycleAware."""

    def __init__(self, schedule_path: Path | None = None) -> None:
        self._schedule_path = schedule_path

    async def start(self) -> None:
        """Start monitoring service."""
        log.info("Monitoring service started")

    async def stop(self) -> None:
        """Stop monitoring service."""
        pass

    async def health_check(self) -> dict:
        """Check monitoring service health."""
        return {"healthy": True, "service": "monitoring"}

    # --- DI surface for route handlers ---

    def load_schedules(self) -> dict:
        """Load heartbeat schedules from JSON file."""
        return _load_schedules(self._schedule_path)

    def save_schedules(self, schedules: dict) -> None:
        """Save heartbeat schedules to JSON file."""
        _save_schedules(schedules, self._schedule_path)
