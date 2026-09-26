"""The session feed: one enriched snapshot of every agent, cached briefly and
pushed to Socket.IO ``/sessions`` subscribers whenever something changed."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import EnrichedAgent, build_session_snapshot

if TYPE_CHECKING:
    import socketio

log = logging.getLogger(__name__)

SESSIONS_NAMESPACE = "/sessions"
SESSIONS_UPDATE_EVENT = "sessions:update"
INBOX_PENDING_EVENT = "inbox:pending"
SNAPSHOT_TTL_SECONDS = 5.0


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
        self._cached_at: float | None = None
        self._lock = asyncio.Lock()
        self._emit_lock = asyncio.Lock()
        self._last_signature: str | None = None
        self._hinted: dict[str, int] = {}
        """The newest inbox row each session was hinted about."""

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
        return self._cached_at is not None and time.monotonic() - self._cached_at < self._ttl

    async def invalidate(self) -> None:
        """Forget the cached snapshot (an agent was started, stopped or edited)."""
        async with self._lock:
            self._cache = []
            self._cached_at = None

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

    async def hint_inbox(self, summary: Mapping[str, tuple[int, int]]) -> None:
        """Tell ``/sessions`` subscribers whose inbox has something new:
        ``inbox:pending {session, pending}``, with no message text, once per
        new row (``summary`` is ``QueueRepo.inbox_summary``: the count and
        the newest row id). A hint can be missed (a disconnect, a restart):
        readers also read their inbox on connect and on a slow poll."""
        fresh = {
            name: pending
            for name, (pending, newest) in summary.items()
            if newest > self._hinted.get(name, 0)
        }
        for name, (_, newest) in summary.items():
            self._hinted[name] = max(newest, self._hinted.get(name, 0))
        if self._sio is None:
            return
        for name, pending in fresh.items():
            await self._sio.emit(
                INBOX_PENDING_EVENT,
                {"session": name, "pending": pending},
                namespace=SESSIONS_NAMESPACE,
            )

    async def refresh_and_emit(self) -> None:
        """After a change made through the API: drop the cache and broadcast."""
        await self.invalidate()
        await self.emit()
