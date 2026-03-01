"""Agent & session management endpoints."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.config import BackboneConfig
from agent_backbone.services.state import get_agent_state
from agent_backbone.services.tmux import (
    capture_pane,
    list_sessions,
    list_sessions_rich,
    session_exists,
)
from api.deps import get_config
from api.models import AgentStateDetail, EnrichedAgent, ListEnvelope, RuntimeInfo

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["agents"])

# Fallback directories for binaries not on system PATH
_FALLBACK_DIRS = [
    Path.home() / ".bun" / "bin",
    Path.home() / ".local" / "bin",
]


def _resolve_command(name: str | None) -> str | None:
    """Resolve a command name to an absolute path.

    Tries shutil.which() first (system PATH), then checks fallback
    directories. Returns the absolute path string, or None if unresolved.
    """
    if name is None:
        return None
    path = shutil.which(name)
    if path:
        return path
    for directory in _FALLBACK_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


# Runtime registry
_RUNTIMES = {
    "claude": {"display_name": "Claude Code", "command": "claude"},
    "gemini": {"display_name": "Gemini CLI", "command": "gemini"},
    "codex": {"display_name": "Codex", "command": "codex"},
    "shell": {"display_name": "Plain Shell", "command": None},
}

# Resolve commands to absolute paths at load time
for _rt_id, _rt_entry in _RUNTIMES.items():
    _rt_entry["resolved_path"] = _resolve_command(_rt_entry["command"])
    if _rt_entry["command"] and not _rt_entry["resolved_path"]:
        log.warning("Runtime '%s': binary '%s' not found", _rt_id, _rt_entry["command"])


async def _build_enriched_agent(
    session: str,
    entity: str,
    config: BackboneConfig,
    active_sessions: set[str],
    tmux_info: dict | None = None,
    agent_type: str = "coding_agent",
) -> EnrichedAgent:
    """Build an EnrichedAgent from session name and entity."""
    online = session in active_sessions
    snapshot = await get_agent_state(
        config.agent_state.state_path,
        session,
        config.agent_state.stale_threshold_seconds,
    )
    reg_entry = config.registry.entities.get(entity)
    display_name = reg_entry.figure.split()[-1] if reg_entry else session
    role = reg_entry.role if reg_entry else "Coding Agent"
    figure = reg_entry.figure if reg_entry else ""
    groups = list(reg_entry.groups) if reg_entry else []
    home = reg_entry.home if reg_entry else ""
    if not home and agent_type == "coding_agent":
        home = config.registry.repo_path_by_name.get(session, "")

    # Resolve org: named entities from registry, coding agents from RepoInfo
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
    if tmux_info:
        created_ts = tmux_info.get("created", 0)
        if created_ts:
            tmux_created = datetime.fromtimestamp(created_ts, tz=UTC).isoformat()
        tmux_attached = tmux_info.get("attached", False)
        tmux_windows = tmux_info.get("windows", 0)

    # State inference: reconcile online (tmux) with state (file)
    state_value = snapshot.state.value
    if not online:
        # No tmux session → offline, regardless of stale state file
        state_value = "offline"
    elif state_value == "unknown":
        # Tmux session exists but no state file → default to idle
        state_value = "idle"

    entity_type = reg_entry.entity_type if reg_entry else "agent"

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
        tmux_attached=tmux_attached,
        tmux_windows=tmux_windows,
    )


# TTL cache for /api/agents — avoids subprocess storms from dashboard polling
_agents_cache: list[EnrichedAgent] = []
_agents_cache_ts: float = 0
_AGENTS_CACHE_TTL = 5.0


@router.get("/agents", response_model=ListEnvelope[EnrichedAgent])
async def list_agents(config: BackboneConfig = Depends(get_config)):
    """List all agents (named entities + discovered coding agents) with live state."""
    global _agents_cache, _agents_cache_ts  # noqa: PLW0603
    now = time.monotonic()
    if now - _agents_cache_ts < _AGENTS_CACHE_TTL and _agents_cache:
        return ListEnvelope(items=_agents_cache, total=len(_agents_cache))

    agents: list[EnrichedAgent] = []

    # Fetch rich session info once — single subprocess call
    rich_sessions = await list_sessions_rich()
    tmux_lookup = {s["name"]: s for s in rich_sessions}
    active_sessions = set(tmux_lookup.keys())

    # Named entities (skip service entities — they have no tmux sessions)
    for entity, session in config.registry.sessions_map.items():
        reg_entry = config.registry.entities.get(entity)
        if reg_entry and reg_entry.entity_type == "service":
            continue
        agent = await _build_enriched_agent(
            session,
            entity,
            config,
            active_sessions,
            tmux_lookup.get(session),
            agent_type="named_entity",
        )
        agents.append(agent)

    # Discover coding agents from tmux sessions (exclude services)
    named_sessions = set(config.registry.sessions_map.values())
    service_sessions = config.entities.service_sessions
    seen_coding: set[str] = set()
    for session in active_sessions:
        if session not in named_sessions and session not in service_sessions:
            agent = await _build_enriched_agent(
                session,
                session,
                config,
                active_sessions,
                tmux_lookup.get(session),
                agent_type="coding_agent",
            )
            agents.append(agent)
            seen_coding.add(session)

    # Include all known repos from filesystem scan (offline ones too)
    for repo in config.registry.repos:
        if repo.name not in seen_coding and repo.name not in named_sessions:
            agent = await _build_enriched_agent(
                repo.name,
                repo.name,
                config,
                active_sessions,
                agent_type="coding_agent",
            )
            agents.append(agent)

    _agents_cache = agents
    _agents_cache_ts = now
    return ListEnvelope(items=agents, total=len(agents))


@router.get("/agents/{session}/state", response_model=AgentStateDetail)
async def get_agent_state_endpoint(session: str, config: BackboneConfig = Depends(get_config)):
    """Get detailed state for a specific agent session."""
    snapshot = await get_agent_state(
        config.agent_state.state_path,
        session,
        config.agent_state.stale_threshold_seconds,
    )
    return AgentStateDetail(
        session=session,
        state=snapshot.state.value,
        current_issue=snapshot.current_issue,
        timestamp=snapshot.timestamp,
        source=snapshot.source,
        started_at=snapshot.started_at,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
    )


@router.get("/runtimes", response_model=list[RuntimeInfo])
async def list_runtimes():
    """List available runtimes for agent sessions."""
    return [
        RuntimeInfo(
            id=k,
            display_name=v["display_name"],
            available=v["command"] is None or v["resolved_path"] is not None,
        )
        for k, v in _RUNTIMES.items()
    ]


@router.get("/sessions", response_model=list[str])
async def get_sessions():
    """List all active tmux sessions."""
    return await list_sessions()


@router.get("/sessions/{name}/terminal")
async def get_terminal_output(name: str, lines: int = Query(default=50, ge=1, le=500)):
    """Capture recent terminal output from a tmux session."""
    output = await capture_pane(name, lines=lines)
    if not output and not await session_exists(name):
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    return {"session": name, "lines": lines, "content": output}
