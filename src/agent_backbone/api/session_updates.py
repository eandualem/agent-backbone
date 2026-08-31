"""Shared session snapshot building and Socket.IO update broadcasting."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_backbone.api.models import EnrichedAgent
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.terminal import (
    RUNTIME_ENV_KEY,
    TmuxService,
    query_environment_var,
)

if TYPE_CHECKING:
    import socketio

log = logging.getLogger(__name__)

SESSIONS_NAMESPACE = "/sessions"
SESSIONS_UPDATE_EVENT = "sessions:update"

_sessions_update_lock = asyncio.Lock()
_last_sessions_update_signature: str | None = None
_snapshot_cache: list[Any] = []
_snapshot_cache_ts: float = 0.0
_snapshot_cache_lock = asyncio.Lock()
_SNAPSHOT_CACHE_TTL = 5.0


async def build_enriched_agent(
    session: str,
    config: BackboneConfig,
    active_sessions: set[str],
    state_svc: StateService,
    tmux_info: dict | None = None,
) -> EnrichedAgent:
    """Build an EnrichedAgent for a session (configured agent or ad-hoc session)."""
    online = session in active_sessions
    snapshot = await state_svc.get_state(session)
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

    state_value = snapshot.state.value
    if not online:
        state_value = "offline"
    elif state_value == "unknown":
        state_value = "idle"

    runtime: str | None = spec.runtime if spec else None
    if online:
        try:
            runtime = await query_environment_var(session, RUNTIME_ENV_KEY) or runtime
        except Exception:
            pass

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
        state=state_value,
        current_issue=snapshot.current_issue,
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


async def build_session_snapshot(
    config: BackboneConfig,
    state_svc: StateService,
    tmux_svc: TmuxService,
) -> list[EnrichedAgent]:
    """Build the full enriched session snapshot without caching."""
    rich_sessions = await tmux_svc.list_sessions_rich()
    tmux_lookup = {session["name"]: session for session in rich_sessions}
    active_sessions = set(tmux_lookup.keys())

    coros = [
        build_enriched_agent(session, config, active_sessions, state_svc, tmux_lookup.get(session))
        for session in listable_sessions(config, active_sessions)
    ]
    return list(await asyncio.gather(*coros))


async def get_cached_session_snapshot(
    build_fn: Callable[[], Awaitable[list[Any]]],
    ttl: float = _SNAPSHOT_CACHE_TTL,
    force_refresh: bool = False,
) -> list[Any]:
    """Return a cached session snapshot, rebuilding under a shared lock when needed."""
    global _snapshot_cache, _snapshot_cache_ts  # noqa: PLW0603

    now = time.monotonic()
    if not force_refresh and now - _snapshot_cache_ts < ttl and _snapshot_cache:
        return _snapshot_cache

    async with _snapshot_cache_lock:
        now = time.monotonic()
        if not force_refresh and now - _snapshot_cache_ts < ttl and _snapshot_cache:
            return _snapshot_cache

        _snapshot_cache = await build_fn()
        _snapshot_cache_ts = now
        return _snapshot_cache


def _invalidate_session_snapshot_caches_unlocked() -> None:
    global _snapshot_cache, _snapshot_cache_ts  # noqa: PLW0603

    _snapshot_cache = []
    _snapshot_cache_ts = 0.0


async def invalidate_session_snapshot_caches() -> None:
    """Reset the shared cached agent snapshot under the shared lock."""
    async with _snapshot_cache_lock:
        _invalidate_session_snapshot_caches_unlocked()


def reset_sessions_update_state() -> None:
    """Reset module-level update dedup state for test isolation."""
    global _last_sessions_update_signature, _sessions_update_lock, _snapshot_cache_lock  # noqa: PLW0603
    _snapshot_cache_lock = asyncio.Lock()
    _invalidate_session_snapshot_caches_unlocked()
    _last_sessions_update_signature = None
    _sessions_update_lock = asyncio.Lock()


def _serialize_snapshot(snapshot: list[EnrichedAgent]) -> list[dict]:
    return [agent.model_dump(mode="json") for agent in snapshot]


def _snapshot_signature(payload: list[dict]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


async def emit_sessions_update(
    sio: socketio.AsyncServer | None,
    config: BackboneConfig,
    state_svc: StateService | None,
    tmux_svc: TmuxService | None,
    *,
    only_if_changed: bool = False,
) -> bool:
    """Broadcast the current enriched session snapshot to `/sessions` subscribers."""
    global _last_sessions_update_signature  # noqa: PLW0603
    if sio is None or state_svc is None or tmux_svc is None:
        return False

    async with _sessions_update_lock:
        snapshot = await get_cached_session_snapshot(
            lambda: build_session_snapshot(config, state_svc, tmux_svc),
            force_refresh=True,
        )
        payload = _serialize_snapshot(snapshot)
        signature = _snapshot_signature(payload)

        if only_if_changed and signature == _last_sessions_update_signature:
            return False

        await sio.emit(SESSIONS_UPDATE_EVENT, payload, namespace=SESSIONS_NAMESPACE)
        _last_sessions_update_signature = signature
        return True
