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
_last_agent_states: dict[str, str] = {}
_snapshot_cache: list[Any] = []
_snapshot_cache_ts: float = 0.0
_snapshot_cache_lock = asyncio.Lock()
_SNAPSHOT_CACHE_TTL = 5.0


async def build_enriched_agent(
    session: str,
    entity: str,
    config: BackboneConfig,
    active_sessions: set[str],
    state_svc: StateService,
    tmux_info: dict | None = None,
    agent_type: str = "coding_agent",
) -> EnrichedAgent:
    """Build an EnrichedAgent from session name and entity."""
    online = session in active_sessions
    if online:
        snapshot = await state_svc.get_state(session)
    else:
        # Avoid syncing stale push snapshots back into the DB for offline
        # sessions. That can overwrite the monitor's "unknown" marker and
        # re-arm repeated offline escalations on every refresh cycle.
        snapshot = state_svc.read_state(session)
        if snapshot is None:
            snapshot = await state_svc.get_state(session)
    reg_entry = config.registry.entry_for_session(session) or config.registry.entities.get(entity)
    display_name = reg_entry.figure.split()[-1] if reg_entry else session
    role = reg_entry.role if reg_entry else "Coding Agent"
    figure = reg_entry.figure if reg_entry else ""
    groups = list(reg_entry.groups) if reg_entry else []
    home = reg_entry.home if reg_entry else ""
    if not home and agent_type == "coding_agent":
        home = config.registry.repo_path_by_name.get(session, "")

    org = ""
    if reg_entry and reg_entry.organization:
        org = reg_entry.organization
    elif agent_type == "coding_agent":
        for repo in config.registry.repos:
            if repo.name == session:
                org = repo.org
                break

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

    entity_type = reg_entry.entity_type if reg_entry else "agent"

    runtime: str | None = None
    if online:
        try:
            runtime = await query_environment_var(session, RUNTIME_ENV_KEY) or None
        except Exception:
            pass

    return EnrichedAgent(
        session=session,
        entity=entity,
        display_name=display_name,
        role=role,
        figure=figure,
        org=org,
        groups=groups,
        home=home,
        type=agent_type,
        entity_type=entity_type,
        state=state_value,
        current_issue=snapshot.current_issue,
        online=online,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
        tmux_created=tmux_created,
        runtime=runtime,
        tmux_attached=tmux_attached,
        tmux_windows=tmux_windows,
        last_activity=last_activity,
        state_since=snapshot.timestamp if snapshot.timestamp else None,
    )


def listable_registry_sessions(config: BackboneConfig) -> dict[str, str]:
    """Concrete registry-backed sessions that should appear as named agents."""
    return config.registry.concrete_sessions_map


def reserved_agent_sessions(config: BackboneConfig) -> set[str]:
    """Sessions reserved by registry-backed agents."""
    return set(listable_registry_sessions(config).values())


async def build_session_snapshot(
    config: BackboneConfig,
    state_svc: StateService,
    tmux_svc: TmuxService,
) -> list[EnrichedAgent]:
    """Build the full enriched session snapshot without caching."""
    rich_sessions = await tmux_svc.list_sessions_rich()
    tmux_lookup = {session["name"]: session for session in rich_sessions}
    active_sessions = set(tmux_lookup.keys())

    coros: list = []

    registry_sessions = listable_registry_sessions(config)
    for entity, session in registry_sessions.items():
        reg_entry = config.registry.entry_for_session(session) or config.registry.entities.get(
            entity
        )
        if reg_entry and reg_entry.entity_type == "service":
            continue
        coros.append(
            build_enriched_agent(
                session,
                entity,
                config,
                active_sessions,
                state_svc,
                tmux_lookup.get(session),
                agent_type="named_entity",
            )
        )

    named_sessions = reserved_agent_sessions(config)
    service_sessions = config.entities.service_sessions
    seen_coding: set[str] = set()
    for session in active_sessions:
        if session not in named_sessions and session not in service_sessions:
            coros.append(
                build_enriched_agent(
                    session,
                    session,
                    config,
                    active_sessions,
                    state_svc,
                    tmux_lookup.get(session),
                    agent_type="coding_agent",
                )
            )
            seen_coding.add(session)

    for repo in config.registry.repos:
        if repo.name not in seen_coding and repo.name not in named_sessions:
            coros.append(
                build_enriched_agent(
                    repo.name,
                    repo.name,
                    config,
                    active_sessions,
                    state_svc,
                    agent_type="coding_agent",
                )
            )

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
    """Reset the shared cached agent snapshot.

    Caller must already hold any required synchronization.
    """
    global _snapshot_cache, _snapshot_cache_ts  # noqa: PLW0603

    _snapshot_cache = []
    _snapshot_cache_ts = 0.0


async def invalidate_session_snapshot_caches() -> None:
    """Reset the shared cached agent snapshot under the shared lock."""
    async with _snapshot_cache_lock:
        _invalidate_session_snapshot_caches_unlocked()


def reset_sessions_update_state() -> None:
    """Reset module-level update dedup state for test isolation."""
    global _last_sessions_update_signature, _sessions_update_lock, _snapshot_cache_lock, _last_agent_states  # noqa: PLW0603
    _snapshot_cache_lock = asyncio.Lock()
    _invalidate_session_snapshot_caches_unlocked()
    _last_sessions_update_signature = None
    _last_agent_states = {}
    _sessions_update_lock = asyncio.Lock()


def _serialize_snapshot(snapshot: list[EnrichedAgent]) -> list[dict]:
    return [agent.model_dump(mode="json") for agent in snapshot]


def _agent_signature(agent_dict: dict) -> str:
    """Compute a stable signature for a single agent's state."""
    return json.dumps(agent_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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
    """Emit session updates with per-agent change detection and room routing.

    Per SUB-8 and SUB-9: only agents whose state changed since last emission
    are included. Updates route to agent:{session} rooms and all-agents room.
    """
    global _last_sessions_update_signature, _last_agent_states  # noqa: PLW0603
    if sio is None or state_svc is None or tmux_svc is None:
        return False

    async with _sessions_update_lock:
        snapshot = await get_cached_session_snapshot(
            lambda: build_session_snapshot(config, state_svc, tmux_svc),
            force_refresh=True,
        )
        payload = _serialize_snapshot(snapshot)

        # SUB-9: Per-agent change detection
        changed_agents: list[dict] = []
        for agent_dict in payload:
            session = agent_dict.get("session", "")
            sig = _agent_signature(agent_dict)
            if _last_agent_states.get(session) != sig:
                _last_agent_states[session] = sig
                changed_agents.append(agent_dict)

        if not changed_agents:
            return False

        # SUB-8: Emit to all-agents room with all changed agents
        await sio.emit(
            SESSIONS_UPDATE_EVENT,
            changed_agents,
            namespace=SESSIONS_NAMESPACE,
            room="all-agents",
        )

        # SUB-8: Emit to per-agent rooms with single-element list
        for agent_dict in changed_agents:
            session = agent_dict.get("session", "")
            if session:
                await sio.emit(
                    SESSIONS_UPDATE_EVENT,
                    [agent_dict],
                    namespace=SESSIONS_NAMESPACE,
                    room=f"agent:{session}",
                )

        _last_sessions_update_signature = _snapshot_signature(payload)
        return True
