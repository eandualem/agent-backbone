"""Agent state tracking service — LifecycleAware wrapper."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents._inference import get_agent_state as _get_agent_state
from agent_backbone.services.agents.models import AgentState, StateSnapshot

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)
_LIVE_RECONCILIATION_STATES = frozenset({AgentState.STARTING, AgentState.BUSY, AgentState.UNKNOWN})


def _row_to_snapshot(row: dict) -> StateSnapshot:
    """Convert a DB agent_states row dict to a StateSnapshot."""
    state = AgentState.parse(row.get("state"))

    ts_raw = row.get("ts")
    timestamp = float(ts_raw) if ts_raw else 0.0

    started_raw = row.get("started_at")
    started_at = float(started_raw) if started_raw else None

    return StateSnapshot(
        state=state,
        reason=row.get("reason") or None,
        current_issue=row.get("current_issue"),
        current_repo=row.get("current_repo"),
        timestamp=timestamp,
        source="db",
        started_at=started_at,
        plan_file=row.get("plan_file"),
        plan_title=row.get("plan_title"),
        evidence=["database snapshot"],
    )


def _should_use_db_snapshot(snapshot: StateSnapshot, trust_seconds: float) -> bool:
    """Whether a persisted snapshot is safe to reuse without live verification."""
    if snapshot.state in _LIVE_RECONCILIATION_STATES:
        return False
    return snapshot.timestamp > 0 and (time.time() - snapshot.timestamp) <= trust_seconds


def _should_sync_db_snapshot(current: StateSnapshot | None, live: StateSnapshot) -> bool:
    """Whether a live reconciliation result should refresh the persisted cache."""
    if current is None or live.state == AgentState.UNKNOWN:
        return False
    return (
        current.state != live.state
        or current.reason != live.reason
        or current.current_issue != live.current_issue
        or current.plan_file != live.plan_file
        or current.plan_title != live.plan_title
    )


class StateService:
    """Agent state tracking service implementing LifecycleAware."""

    def __init__(
        self,
        state_dir: str | Path,
        stale_threshold: int = 300,
        db: BackboneDB | None = None,
        snapshot_trust: int = 20,
    ) -> None:
        self._state_dir = Path(state_dir).expanduser()
        self._stale_threshold = stale_threshold
        self._snapshot_trust = snapshot_trust
        self._db = db

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    async def start(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        log.info("State service started: state_dir=%s, db=%s", self._state_dir, bool(self._db))

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {
            "healthy": self._state_dir.is_dir(),
            "service": "state",
            "state_dir": str(self._state_dir),
        }

    # --- DI surface for route handlers ---

    async def get_state(self, session: str) -> StateSnapshot:
        """Get reconciled agent state.

        A hook-written state file fresher than the stored snapshot is
        authoritative, so the DB shortcut only applies when no newer hook
        state exists and the snapshot is recent enough to trust.
        """
        db_snapshot: StateSnapshot | None = None
        if self._db is not None:
            try:
                row = await self._db.get_agent_state(session)
                if row is not None:
                    db_snapshot = _row_to_snapshot(row)
                    push = read_state_file(self._state_dir, session)
                    push_is_newer = push is not None and push.timestamp > db_snapshot.timestamp
                    if not push_is_newer and _should_use_db_snapshot(
                        db_snapshot, self._snapshot_trust
                    ):
                        return db_snapshot
            except Exception:
                log.warning("DB state read failed for %s, falling back to file", session)
        live_snapshot = await _get_agent_state(self._state_dir, session, self._stale_threshold)
        if self._db is not None and _should_sync_db_snapshot(db_snapshot, live_snapshot):
            try:
                await self._db.set_agent_state(session, **live_snapshot.db_fields())
            except Exception:
                log.warning("DB state refresh failed for %s after live reconciliation", session)
        return live_snapshot

    def read_state(self, session: str) -> StateSnapshot | None:
        """Read push-based state file for a session."""
        return read_state_file(self._state_dir, session)
