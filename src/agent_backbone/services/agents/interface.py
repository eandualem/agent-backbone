"""Agent state tracking service and monitoring coordination service — LifecycleAware wrappers."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents._heartbeat import (
    load_schedules as _load_schedules,
)
from agent_backbone.services.agents._heartbeat import (
    save_schedules as _save_schedules,
)
from agent_backbone.services.agents._inference import get_agent_state as _get_agent_state
from agent_backbone.services.agents.models import AgentState, StateSnapshot

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)
_LIVE_RECONCILIATION_STATES = frozenset(
    {
        AgentState.STARTING,
        AgentState.BUSY,
        AgentState.PROCESSING_ISSUE,
        AgentState.UNKNOWN,
    }
)

# Pushed states are authoritative for this duration (seconds).
# Pane inference only kicks in after this window expires.
_PUSH_AUTHORITY_SECONDS = 60.0


def _row_to_snapshot(row: dict) -> StateSnapshot:
    """Convert a DB agent_states row dict to a StateSnapshot."""
    try:
        state = AgentState(row.get("state", "unknown"))
    except ValueError:
        state = AgentState.UNKNOWN

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


def _should_use_db_snapshot(snapshot: StateSnapshot, *, recently_pushed: bool = False) -> bool:
    """Whether a persisted snapshot is safe to reuse without live verification.

    Recently pushed states are always trusted — they come from authoritative
    hooks. Older states in working categories trigger live reconciliation.
    """
    if recently_pushed:
        return True
    return snapshot.state not in _LIVE_RECONCILIATION_STATES


# Module-level registry of recent authoritative pushes: session → monotonic timestamp.
# Populated by record_push(), checked by get_state().
_push_timestamps: dict[str, float] = {}


def record_push(session: str) -> None:
    """Mark a session as having received an authoritative push.

    Called from the POST /api/agents/{session}/state route handler.
    """
    _push_timestamps[session] = time.monotonic()


def reset_push_timestamps() -> None:
    """Clear push authority tracking (for test isolation)."""
    _push_timestamps.clear()


def _is_recently_pushed(session: str) -> bool:
    """Whether the session received an authoritative push within the authority window."""
    ts = _push_timestamps.get(session)
    if ts is None:
        return False
    return (time.monotonic() - ts) < _PUSH_AUTHORITY_SECONDS


def _should_sync_db_snapshot(current: StateSnapshot | None, live: StateSnapshot) -> bool:
    """Whether a live reconciliation result should refresh the persisted cache."""
    if current is None or live.state == AgentState.UNKNOWN:
        return False
    return (
        current.state != live.state
        or current.current_issue != live.current_issue
        or current.plan_file != live.plan_file
        or current.plan_title != live.plan_title
    )


class StateService:
    """Agent state tracking service implementing LifecycleAware."""

    def __init__(
        self,
        state_dir: str = "~/.claude/state",
        stale_threshold: int = 300,
        db: BackboneDB | None = None,
    ) -> None:
        self._state_dir = Path(state_dir).expanduser()
        self._stale_threshold = stale_threshold
        self._db = db

    async def start(self) -> None:
        log.info("State service started: state_dir=%s, db=%s", self._state_dir, bool(self._db))

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {
            "healthy": self._state_dir.is_dir(),
            "service": "state",
            "state_dir_exists": self._state_dir.is_dir(),
        }

    # --- DI surface for route handlers ---

    async def get_state(self, session: str) -> StateSnapshot:
        """Get reconciled agent state — DB first, file+tmux fallback.

        Recently pushed states (within _PUSH_AUTHORITY_SECONDS) are trusted
        without pane inference. Pane inference only applies when the DB
        state is stale or absent.
        """
        db_snapshot: StateSnapshot | None = None
        if self._db is not None:
            try:
                row = await self._db.get_agent_state(session)
                if row is not None:
                    db_snapshot = _row_to_snapshot(row)
                    recently_pushed = _is_recently_pushed(session)
                    if _should_use_db_snapshot(db_snapshot, recently_pushed=recently_pushed):
                        return db_snapshot
            except Exception:
                log.warning("DB state read failed for %s, falling back to file", session)
        live_snapshot = await _get_agent_state(self._state_dir, session, self._stale_threshold)
        if self._db is not None and _should_sync_db_snapshot(db_snapshot, live_snapshot):
            try:
                await self._db.set_agent_state(
                    session,
                    live_snapshot.state.value,
                    current_issue=live_snapshot.current_issue,
                    ts=str(live_snapshot.timestamp) if live_snapshot.timestamp else None,
                    plan_file=live_snapshot.plan_file,
                    plan_title=live_snapshot.plan_title,
                )
            except Exception:
                log.warning("DB state refresh failed for %s after live reconciliation", session)
        return live_snapshot

    def read_state(self, session: str) -> StateSnapshot | None:
        """Read push-based state file for a session."""
        return read_state_file(self._state_dir, session)


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
