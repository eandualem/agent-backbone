"""Agent & session management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_backbone.api.deps import get_config, get_db, get_state_service, get_tmux_service
from agent_backbone.api.models import (
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
    emit_sessions_update,
    get_cached_session_snapshot,
    invalidate_session_snapshot_caches,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.infrastructure._agents import (
    RUNTIME_COMMANDS,
    RUNTIME_DISPLAY_NAMES,
    build_command,
    launch_environment,
    runtime_available,
)
from agent_backbone.services.terminal import TmuxService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["agents"])


async def _broadcast(request: Request, config: BackboneConfig, tmux_svc: TmuxService) -> None:
    await invalidate_session_snapshot_caches()
    await emit_sessions_update(
        getattr(request.app.state, "sio", None),
        config,
        getattr(request.app.state, "state_service", None),
        tmux_svc,
    )


@router.get("/agents", response_model=ListEnvelope[EnrichedAgent])
async def list_agents(
    config: BackboneConfig = Depends(get_config),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """List configured agents (plus other live tmux sessions) with live state."""
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
    """List supported runtimes and whether their binary is installed."""
    return [
        RuntimeInfo(id=k, display_name=RUNTIME_DISPLAY_NAMES[k], available=runtime_available(k))
        for k in RUNTIME_COMMANDS
    ]


@router.post("/agents/{session}/start", response_model=AgentStartResponse)
async def start_agent(
    request: Request,
    session: str,
    body: AgentStartRequest | None = None,
    config: BackboneConfig = Depends(get_config),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Start an agent session.

    Configured agents use their ``[agents.<name>]`` settings; request fields
    override them. Unconfigured sessions require ``working_directory``.
    """
    req = body or AgentStartRequest()
    spec = config.agents.get(session)

    runtime = req.runtime or (spec.runtime if spec else "claude")
    model = req.model if req.model is not None else (spec.model if spec else None)
    working_dir = req.working_directory or (str(spec.path) if spec else None)

    if runtime not in RUNTIME_COMMANDS:
        raise HTTPException(status_code=400, detail=f"Unknown runtime: {runtime}")
    if not runtime_available(runtime):
        raise HTTPException(status_code=400, detail=f"Runtime '{runtime}' binary not found")

    if await tmux_svc.session_exists(session):
        return AgentStartResponse(
            ok=True,
            session=session,
            runtime=runtime,
            model=model,
            already_existed=True,
        )

    if not working_dir:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{session}' is not a configured agent — provide working_directory "
                "or add it to backbone.toml"
            ),
        )

    command = build_command(runtime, model=model, resume=req.resume)
    environment = launch_environment(session, runtime, config.state_dir, spec.env if spec else None)
    ok = await tmux_svc.start_session(
        session,
        working_dir=working_dir,
        command=command,
        environment=environment,
    )
    if ok:
        await _broadcast(request, config, tmux_svc)
    return AgentStartResponse(
        ok=ok,
        session=session,
        working_directory=working_dir,
        runtime=runtime,
        model=model if command is not None else None,
    )


@router.post("/agents/{session}/stop", response_model=AgentStopResponse)
async def stop_agent(
    request: Request,
    session: str,
    config: BackboneConfig = Depends(get_config),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Stop an agent tmux session."""
    if session == config.backbone.session_name:
        raise HTTPException(status_code=400, detail="Refusing to stop the backbone's own session")
    ok = await tmux_svc.stop_session(session)
    if ok:
        await _broadcast(request, config, tmux_svc)
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


@router.post("/agents/{session}/state")
async def post_agent_state(
    request: Request,
    session: str,
    body: StateUpdateRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Update agent state (called by runtime hooks)."""
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
    await _broadcast(request, config, getattr(request.app.state, "tmux_service", None))
    return {"ok": True, "session": session}
