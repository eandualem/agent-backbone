"""Agent & session management endpoints."""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_backbone.api.data_streams import notify_stream
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
from agent_backbone.api.session_updates import (
    build_session_snapshot,
    emit_session_override,
    emit_sessions_update,
    get_cached_session_snapshot,
    invalidate_session_snapshot_caches,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import AgentState, StateService, StateSnapshot
from agent_backbone.services.agents._observation import enrich_idle_state
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.terminal import (
    RUNTIME_ENV_KEY,
    TmuxService,
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


_STATE_ALIASES = {
    "processing_issue": AgentState.BUSY.value,
    "unknown": AgentState.OFFLINE.value,
}
_SPECIFIC_STATE_EVENTS = {
    AgentState.IDLE.value: "agent.idle",
    AgentState.STARTING.value: "agent.starting",
    AgentState.BUSY.value: "agent.busy",
    AgentState.PLAN_WAITING.value: "agent.plan_waiting",
    AgentState.PERMISSION_WAITING.value: "agent.permission_waiting",
    AgentState.SUB_AGENT_WAITING.value: "agent.sub_agent_waiting",
    AgentState.OFFLINE.value: "agent.offline",
}


def _entity_for_session(
    config: BackboneConfig,
    session: str,
    body: StateUpdateRequest | None = None,
) -> str:
    """Resolve the canonical entity name for a session."""
    if body and body.entity:
        return body.entity
    return config.registry.entity_by_session.get(session, session)


def _normalized_state_value(body: StateUpdateRequest) -> str:
    """Normalize the incoming hook/API state value to the active taxonomy."""
    hook_event = body.hook_event.strip()
    raw_state = (body.state or "").strip().lower()
    if hook_event == "PermissionRequest":
        raw_state = AgentState.PERMISSION_WAITING.value
    state = _STATE_ALIASES.get(raw_state, raw_state)
    try:
        return AgentState(state).value
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown state: {body.state}") from exc


async def _build_state_snapshot(
    session: str,
    body: StateUpdateRequest,
) -> StateSnapshot:
    """Normalize and enrich an incoming state update."""
    timestamp = body.ts if body.ts > 0 else time.time()
    snapshot = StateSnapshot(
        state=AgentState(_normalized_state_value(body)),
        current_issue=body.issue,
        timestamp=timestamp,
        source="hook",
        plan_file=body.plan_file,
        plan_title=body.plan_title,
    )
    if snapshot.state == AgentState.IDLE:
        snapshot = await enrich_idle_state(session, snapshot)
    return snapshot

@router.get("/agents", response_model=ListEnvelope[EnrichedAgent])
async def list_agents(
    config: BackboneConfig = Depends(get_config),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """List all agents (named entities + discovered coding agents) with live state."""
    agents = await get_cached_session_snapshot(
        lambda: build_session_snapshot(config, state_svc, tmux_svc)
    )
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
    request: Request,
    session: str,
    body: AgentStartRequest | None = None,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
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
    if ok:
        await db.set_agent_state(
            session,
            AgentState.STARTING.value,
            current_issue=None,
            entity=_entity_for_session(config, session),
            context=None,
            ts=str(time.time()),
            plan_file=None,
            plan_title=None,
        )
        await invalidate_session_snapshot_caches()
        await emit_sessions_update(
            getattr(request.app.state, "sio", None),
            config,
            getattr(request.app.state, "state_service", None),
            tmux_svc,
        )
    await notify_stream("agents")
    return AgentStartResponse(
        ok=ok,
        session=session,
        working_directory=working_dir,
        runtime=req.runtime,
        model=req.model if rt["command"] is not None else None,
    )


@router.post("/agents/{session}/stop", response_model=AgentStopResponse)
async def stop_agent(
    request: Request,
    session: str,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Stop an agent tmux session."""
    ok = await tmux_svc.stop_session(session)
    if ok:
        await db.set_agent_state(
            session,
            AgentState.OFFLINE.value,
            current_issue=None,
            entity=_entity_for_session(config, session),
            context=None,
            ts=str(time.time()),
            plan_file=None,
            plan_title=None,
        )
        await invalidate_session_snapshot_caches()
        await emit_sessions_update(
            getattr(request.app.state, "sio", None),
            config,
            getattr(request.app.state, "state_service", None),
            tmux_svc,
        )
    await notify_stream("agents")
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
    request: Request,
    session: str,
    body: StateUpdateRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Update agent state in the database."""
    entity = _entity_for_session(config, session, body)
    snapshot = await _build_state_snapshot(session, body)

    await emit_session_override(
        getattr(request.app.state, "sio", None),
        config,
        getattr(request.app.state, "state_service", None),
        getattr(request.app.state, "tmux_service", None),
        session=session,
        entity=entity,
        snapshot=snapshot,
    )

    await db.set_agent_state(
        session,
        snapshot.state.value,
        current_issue=snapshot.current_issue,
        entity=entity,
        context=body.context or None,
        ts=str(snapshot.timestamp),
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
    )
    await invalidate_session_snapshot_caches()

    from agent_backbone.api.run_events import emit_run_event

    await emit_run_event(
        "agent.state_changed",
        context={"session": session, "entity": entity},
        source=entity,
        data={
            "state": snapshot.state.value,
            "issue": snapshot.current_issue,
            "context": body.context,
            "hook_event": body.hook_event,
            "cli": body.cli,
        },
        sio=getattr(request.app.state, "sio", None),
    )
    specific_event = _SPECIFIC_STATE_EVENTS.get(snapshot.state.value)
    if specific_event is not None:
        await emit_run_event(
            specific_event,
            context={"session": session, "entity": entity},
            source=entity,
            data={"issue": snapshot.current_issue, "hook_event": body.hook_event, "cli": body.cli},
            sio=getattr(request.app.state, "sio", None),
        )

    await notify_stream("agents")
    await notify_stream("plans")
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
    await notify_stream("activity")
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
