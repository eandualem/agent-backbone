"""The session feed: one enriched snapshot of every agent, cached briefly and
pushed to Socket.IO ``/sessions`` subscribers whenever something changed."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_backbone.api.models import EnrichedAgent
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import agent_state
from agent_backbone.services.runtimes import RUNTIME_ENV_KEY
from agent_backbone.services.terminal import list_sessions_rich, query_environment_var

if TYPE_CHECKING:
    import socketio

log = logging.getLogger(__name__)

SESSIONS_NAMESPACE = "/sessions"
SESSIONS_UPDATE_EVENT = "sessions:update"
SNAPSHOT_TTL_SECONDS = 5.0


async def build_enriched_agent(
    session: str,
    config: BackboneConfig,
    active_sessions: set[str],
    tmux_info: dict | None = None,
) -> EnrichedAgent:
    """Build an EnrichedAgent for a session (configured agent or ad-hoc session)."""
    online = session in active_sessions
    snapshot = await agent_state(config, session)
    spec = config.agents.get(session)

    tmux_created = None
    tmux_attached = False
    tmux_windows = 0
    last_activity: float | None = None
    if tmux_info:
        created_ts = tmux_info.get("created", 0)
        if created_ts:
            tmux_created = datetime.fromtimestamp(created_ts, tz=UTC).isoformat()
        tmux_attached = tmux_info.get("attached", False)
        tmux_windows = tmux_info.get("windows", 0)
        activity_ts = tmux_info.get("activity", 0)
        if activity_ts:
            last_activity = float(activity_ts)

    state_value = snapshot.state.value if online else "offline"

    runtime: str | None = spec.runtime if spec else None
    if online:
        with contextlib.suppress(Exception):
            runtime = await query_environment_var(session, RUNTIME_ENV_KEY) or runtime

    return EnrichedAgent(
        name=session,
        session=session,
        configured=spec is not None,
        runtime=runtime,
        model=spec.model if spec else None,
        dir=str(spec.path) if spec else "",
        repo=spec.repo if spec else "",
        tags=list(spec.tags) if spec else [],
        description=spec.description if spec else "",
        watches=list(spec.watches) if spec else [],
        state=state_value,
        reason=snapshot.reason if online else None,
        current_issue=snapshot.current_issue,
        current_repo=snapshot.current_repo,
        online=online,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
        tmux_created=tmux_created,
        tmux_attached=tmux_attached,
        tmux_windows=tmux_windows,
        last_activity=last_activity,
        state_since=snapshot.timestamp if snapshot.timestamp else None,
    )


def listable_sessions(config: BackboneConfig, active_sessions: set[str]) -> list[str]:
    """Configured agents first, then any other active tmux session (minus the backbone's own)."""
    names = list(config.agents.names)
    hidden = {config.backbone.session_name}
    names.extend(sorted(s for s in active_sessions if s not in config.agents and s not in hidden))
    return names


async def build_session_snapshot(config: BackboneConfig) -> list[EnrichedAgent]:
    """The full enriched snapshot of every listable session, uncached."""
    rich_sessions = await list_sessions_rich()
    tmux_lookup = {session["name"]: session for session in rich_sessions}
    active_sessions = set(tmux_lookup.keys())
    coros = [
        build_enriched_agent(session, config, active_sessions, tmux_lookup.get(session))
        for session in listable_sessions(config, active_sessions)
    ]
    return list(await asyncio.gather(*coros))


class SessionFeed:
    """A cached session snapshot and the Socket.IO broadcast of it.

    ``config`` is a provider so the feed always reads the latest published
    configuration; ``sio`` may be None (no server — nothing is emitted).
    """

    def __init__(
        self,
        config: Callable[[], BackboneConfig],
        sio: socketio.AsyncServer | None = None,
        *,
        ttl_seconds: float = SNAPSHOT_TTL_SECONDS,
    ) -> None:
        self._config = config
        self._sio = sio
        self._ttl = ttl_seconds
        self._cache: list[EnrichedAgent] = []
        self._cached_at = 0.0
        self._lock = asyncio.Lock()
        self._emit_lock = asyncio.Lock()
        self._last_signature: str | None = None

    @property
    def sio(self) -> socketio.AsyncServer | None:
        return self._sio

    @sio.setter
    def sio(self, server: socketio.AsyncServer | None) -> None:
        self._sio = server

    async def snapshot(self, *, force_refresh: bool = False) -> list[EnrichedAgent]:
        """The snapshot, rebuilt when older than the TTL (or when forced)."""
        if not force_refresh and self._fresh():
            return self._cache
        async with self._lock:
            if not force_refresh and self._fresh():
                return self._cache
            self._cache = await build_session_snapshot(self._config())
            self._cached_at = time.monotonic()
            return self._cache

    def _fresh(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < self._ttl

    async def invalidate(self) -> None:
        """Forget the cached snapshot (an agent was started, stopped or edited)."""
        async with self._lock:
            self._cache = []
            self._cached_at = 0.0

    async def emit(self, *, only_if_changed: bool = False) -> bool:
        """Rebuild the snapshot and broadcast it to ``/sessions`` subscribers.

        With ``only_if_changed`` (the monitor tick) nothing is sent when the
        payload equals the last one broadcast.
        """
        if self._sio is None:
            return False
        async with self._emit_lock:
            payload = [
                agent.model_dump(mode="json") for agent in await self.snapshot(force_refresh=True)
            ]
            signature = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            if only_if_changed and signature == self._last_signature:
                return False
            await self._sio.emit(SESSIONS_UPDATE_EVENT, payload, namespace=SESSIONS_NAMESPACE)
            self._last_signature = signature
            return True

    async def refresh_and_emit(self) -> None:
        """After a change made through the API: drop the cache and broadcast."""
        await self.invalidate()
        await self.emit()
