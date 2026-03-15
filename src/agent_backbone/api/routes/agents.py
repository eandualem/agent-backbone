"""Agent & session management endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.api.deps import get_config, get_db, get_state_service, get_tmux_service
from agent_backbone.api.models import (
    ActivityCreateRequest,
    AgentActivityEvent,
    AgentStartRequest,
    AgentStartResponse,
    AgentStateDetail,
    AgentStopResponse,
    EnrichedAgent,
    ListEnvelope,
    RuntimeInfo,
    StateUpdateRequest,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.terminal import (
    RUNTIME_ENV_KEY,
    TmuxService,
    query_environment_var,
    resolve_agent_dir,
)

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
    "cursor": {"display_name": "Cursor Agent", "command": "cursor"},
    "opencode": {"display_name": "OpenCode", "command": "opencode"},
    "aider": {"display_name": "Aider", "command": "aider"},
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
    state_svc: StateService,
    tmux_info: dict | None = None,
    agent_type: str = "coding_agent",
) -> EnrichedAgent:
    """Build an EnrichedAgent from session name and entity."""
    online = session in active_sessions
    snapshot = await state_svc.get_state(session)
    reg_entry = config.registry.entry_for_session(session) or config.registry.entities.get(entity)
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

    # State inference: reconcile online (tmux) with state (file)
    state_value = snapshot.state.value
    if not online:
        # No tmux session → offline, regardless of stale state file
        state_value = "offline"
    elif state_value == "unknown":
        # Tmux session exists but no state file → default to idle
        state_value = "idle"

    entity_type = reg_entry.entity_type if reg_entry else "agent"

    # Resolve runtime for online sessions via tmux environment variable
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


# TTL cache for /api/agents — avoids subprocess storms from dashboard polling
_agents_cache: list[EnrichedAgent] = []
_agents_cache_ts: float = 0
_AGENTS_CACHE_TTL = 5.0
_agents_cache_lock = asyncio.Lock()


def _listable_registry_sessions(config: BackboneConfig) -> dict[str, str]:
    """Concrete registry-backed sessions that should appear as named agents."""
    return config.registry.concrete_sessions_map


def _reserved_agent_sessions(config: BackboneConfig) -> set[str]:
    """Sessions reserved by registry-backed agents."""
    return set(_listable_registry_sessions(config).values())


@router.get("/agents", response_model=ListEnvelope[EnrichedAgent])
async def list_agents(
    config: BackboneConfig = Depends(get_config),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """List all agents (named entities + discovered coding agents) with live state."""
    global _agents_cache, _agents_cache_ts  # noqa: PLW0603
    now = time.monotonic()
    if now - _agents_cache_ts < _AGENTS_CACHE_TTL and _agents_cache:
        return ListEnvelope(items=_agents_cache, total=len(_agents_cache))

    async with _agents_cache_lock:
        # Re-check after acquiring lock (another request may have populated)
        now = time.monotonic()
        if now - _agents_cache_ts < _AGENTS_CACHE_TTL and _agents_cache:
            return ListEnvelope(items=_agents_cache, total=len(_agents_cache))

        # Fetch rich session info once — single subprocess call
        rich_sessions = await tmux_svc.list_sessions_rich()
        tmux_lookup = {s["name"]: s for s in rich_sessions}
        active_sessions = set(tmux_lookup.keys())

        # Collect all coroutines, then execute concurrently with asyncio.gather
        coros: list = []

        # Registry-backed entities, including concrete role-instance sessions.
        registry_sessions = _listable_registry_sessions(config)
        for entity, session in registry_sessions.items():
            reg_entry = config.registry.entry_for_session(session) or config.registry.entities.get(
                entity
            )
            if reg_entry and reg_entry.entity_type == "service":
                continue
            coros.append(
                _build_enriched_agent(
                    session,
                    entity,
                    config,
                    active_sessions,
                    state_svc,
                    tmux_lookup.get(session),
                    agent_type="named_entity",
                )
            )

        # Discover coding agents from tmux sessions (exclude services)
        named_sessions = _reserved_agent_sessions(config)
        service_sessions = config.entities.service_sessions
        seen_coding: set[str] = set()
        for session in active_sessions:
            if session not in named_sessions and session not in service_sessions:
                coros.append(
                    _build_enriched_agent(
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

        # Include all known repos from filesystem scan (offline ones too)
        for repo in config.registry.repos:
            if repo.name not in seen_coding and repo.name not in named_sessions:
                coros.append(
                    _build_enriched_agent(
                        repo.name,
                        repo.name,
                        config,
                        active_sessions,
                        state_svc,
                        agent_type="coding_agent",
                    )
                )

        agents: list[EnrichedAgent] = list(await asyncio.gather(*coros))

        _agents_cache = agents
        _agents_cache_ts = now
        return ListEnvelope(items=agents, total=len(agents))


@router.get("/agents/{session}/state", response_model=AgentStateDetail)
async def get_agent_state_endpoint(
    session: str,
    state_svc: StateService = Depends(get_state_service),
):
    """Get detailed state for a specific agent session."""
    snapshot = await state_svc.get_state(session)
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


@router.post("/agents/{session}/start", response_model=AgentStartResponse)
async def start_agent(
    session: str,
    body: AgentStartRequest | None = None,
    config: BackboneConfig = Depends(get_config),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Start an agent tmux session with a specified runtime."""
    req = body or AgentStartRequest()

    # Validate runtime
    rt = _RUNTIMES.get(req.runtime)
    if rt is None:
        raise HTTPException(status_code=400, detail=f"Unknown runtime: {req.runtime}")

    # Check binary availability (shell has no binary)
    if rt["command"] is not None and not rt["resolved_path"]:
        raise HTTPException(
            status_code=400,
            detail=f"Runtime '{req.runtime}' binary not found: {rt['command']}",
        )

    # Idempotent: if session already exists, return success
    if await tmux_svc.session_exists(session):
        return AgentStartResponse(
            ok=True,
            session=session,
            runtime=req.runtime,
            model=req.model,
            already_existed=True,
        )

    # Resolve working directory
    working_dir = req.working_directory or resolve_agent_dir(session, config.registry)
    if not working_dir:
        raise HTTPException(
            status_code=400,
            detail="Cannot resolve working directory — provide working_directory explicitly",
        )

    # Build command list
    command: list[str] | None = None
    if rt["command"] is not None:
        resolved = rt["resolved_path"]
        command = [resolved]
        if req.model:
            command.extend(["--model", req.model])
        if req.resume:
            command.append("--resume")

    ok = await tmux_svc.start_session(
        session,
        working_dir=working_dir,
        command=command,
        environment={RUNTIME_ENV_KEY: req.runtime},
    )
    return AgentStartResponse(
        ok=ok,
        session=session,
        working_directory=working_dir,
        runtime=req.runtime,
        model=req.model if rt["command"] is not None else None,
    )


@router.post("/agents/{session}/stop", response_model=AgentStopResponse)
async def stop_agent(
    session: str,
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Stop an agent tmux session."""
    ok = await tmux_svc.stop_session(session)
    return AgentStopResponse(ok=ok, session=session)


@router.get("/sessions", response_model=list[str])
async def get_sessions(tmux_svc: TmuxService = Depends(get_tmux_service)):
    """List all active tmux sessions."""
    return await tmux_svc.list_sessions()


@router.get("/sessions/{name}/terminal")
async def get_terminal_output(
    name: str,
    lines: int = Query(default=50, ge=1, le=500),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Capture recent terminal output from a tmux session."""
    output = await tmux_svc.capture_pane(name, lines=lines)
    if not output and not await tmux_svc.session_exists(name):
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    return {"session": name, "lines": lines, "content": output}


# ---------------------------------------------------------------------------
# POST /api/agents/{session}/state
# ---------------------------------------------------------------------------


@router.post("/agents/{session}/state")
async def post_agent_state(
    session: str,
    body: StateUpdateRequest,
    db: BackboneDB = Depends(get_db),
):
    """Update agent state in the database."""
    global _agents_cache_ts  # noqa: PLW0603
    await db.set_agent_state(
        session,
        body.state,
        current_issue=body.issue,
        entity=body.entity or None,
        context=body.context or None,
        ts=str(body.ts) if body.ts else None,
        plan_file=body.plan_file,
        plan_title=body.plan_title,
    )
    _agents_cache_ts = 0
    return {"ok": True, "session": session}


# ---------------------------------------------------------------------------
# POST/GET /api/agents/{session}/activity
# ---------------------------------------------------------------------------


@router.post("/agents/{session}/activity")
async def post_agent_activity(
    session: str,
    body: ActivityCreateRequest,
    db: BackboneDB = Depends(get_db),
):
    """Record an agent activity event."""
    # Extra fields beyond event/ts go into the data payload
    data_dict = dict(body.model_extra or {})
    data_json = json.dumps(data_dict) if data_dict else None
    row_id = await db.record_activity(session, body.event, data_json, str(body.ts))
    return {"ok": True, "id": row_id}


@router.get("/agents/{session}/activity", response_model=ListEnvelope[AgentActivityEvent])
async def get_agent_activity(
    session: str,
    limit: int = Query(default=50, ge=1, le=500),
    since: float | None = Query(default=None),
    db: BackboneDB = Depends(get_db),
):
    """Get activity events for an agent session."""
    since_str = str(since) if since is not None else None
    rows = await db.get_activity(session, limit, since=since_str)
    items = []
    for row in rows:
        data_raw = row.get("data")
        data_dict = None
        if data_raw:
            try:
                data_dict = json.loads(data_raw)
            except (json.JSONDecodeError, TypeError):
                data_dict = {"raw": data_raw}
        ts_val = row.get("ts", "0")
        items.append(
            AgentActivityEvent(
                id=row["id"],
                session=row["session"],
                event=row["event"],
                data=data_dict,
                ts=float(ts_val),
                received_at=row["received_at"],
            )
        )
    return ListEnvelope(items=items, total=len(items))
