"""Agent endpoints — discover, start, stop, inspect, edit."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_backbone.api.deps import (
    get_agent_store,
    get_config,
    get_db,
    get_state_service,
    get_tmux_service,
    registered_agent_or_404,
)
from agent_backbone.api.models import (
    AgentApproveRequest,
    AgentApproveResponse,
    AgentConfigResponse,
    AgentInspectResponse,
    AgentStartRequest,
    AgentStartResponse,
    AgentStateDetail,
    AgentStopResponse,
    AgentUpdateRequest,
    DeliveryRecord,
    EnrichedAgent,
    ListEnvelope,
    RuntimeInfo,
    StateUpdateRequest,
    WatchRequest,
)
from agent_backbone.api.session_updates import (
    build_session_snapshot,
    emit_sessions_update,
    get_cached_session_snapshot,
    invalidate_session_snapshot_caches,
)
from agent_backbone.config import AgentSpec, BackboneConfig
from agent_backbone.services.agent_store import AgentStore
from agent_backbone.services.agents import StateService, read_state_file, write_state_file
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.infrastructure import (
    RUNTIME_COMMANDS,
    RUNTIME_DISPLAY_NAMES,
    approve_agent,
    runtime_available,
    start_agent,
)
from agent_backbone.services.routing._intelligence import get_session_intelligence
from agent_backbone.services.terminal import (
    SESSION_FORMAT_STR,
    TmuxService,
    capture_pane,
    query_format_vars,
    sanitize_pane_content,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["agents"])


async def _broadcast(request: Request, config: BackboneConfig, tmux_svc: TmuxService) -> None:
    await invalidate_session_snapshot_caches()
    await emit_sessions_update(
        getattr(request.app.state, "sio", None),
        request.app.state.config if hasattr(request.app.state, "config") else config,
        getattr(request.app.state, "state_service", None),
        tmux_svc,
    )


@router.get("/agents", response_model=ListEnvelope[EnrichedAgent])
async def list_agents(
    config: BackboneConfig = Depends(get_config),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Known agents (plus other live tmux sessions) with live state."""
    agents = await get_cached_session_snapshot(
        lambda: build_session_snapshot(config, state_svc, tmux_svc)
    )
    return ListEnvelope(items=agents, total=len(agents))


@router.get("/agents/{session}/state", response_model=AgentStateDetail)
async def get_agent_state_endpoint(
    session: str,
    state_svc: StateService = Depends(get_state_service),
):
    """Reconciled state for one agent."""
    snapshot = await state_svc.get_state(session)
    return AgentStateDetail(
        session=session,
        state=snapshot.state.value,
        reason=snapshot.reason,
        current_issue=snapshot.current_issue,
        current_repo=snapshot.current_repo,
        timestamp=snapshot.timestamp,
        source=snapshot.source,
        started_at=snapshot.started_at,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
        evidence=list(snapshot.evidence),
    )


@router.get("/agents/{name}/inspect", response_model=AgentInspectResponse)
async def inspect_agent(
    name: str,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Everything the backbone knows about an agent, with the evidence behind it."""
    spec = config.agents.get(name)
    online = await tmux_svc.session_exists(name)
    profile = await get_session_intelligence(name, config)

    tmux_vars: dict = dict(profile.tmux_vars)
    pane_tail: list[str] = []
    if online:
        try:
            if not tmux_vars:
                tmux_vars = await query_format_vars(name, SESSION_FORMAT_STR)
            pane = await capture_pane(name, lines=40)
            pane_tail = [
                ln.rstrip() for ln in sanitize_pane_content(pane).splitlines() if ln.strip()
            ][-12:]
        except Exception:
            log.debug("inspect: tmux read failed for %s", name)

    state_age: float | None = None
    push = read_state_file(config.state_dir, name)
    if push and push.timestamp:
        state_age = round(time.time() - push.timestamp, 1)

    try:
        recent = await db.query_deliveries(session_name=name, limit=10)
    except Exception:
        recent = []

    return AgentInspectResponse(
        name=name,
        known=spec is not None,
        online=online,
        dir=str(spec.path) if spec else "",
        runtime=profile.runtime if online else (spec.runtime if spec else ""),
        model=spec.model if spec else None,
        repo=spec.repo if spec else "",
        watches=list(spec.watches) if spec else [],
        state=profile.agent_state.value if online else "offline",
        reason=profile.reason,
        current_issue=profile.current_issue,
        current_repo=profile.current_repo,
        state_source=profile.state_source,
        state_age_seconds=state_age,
        delivery=profile.intelligence.value,
        evidence=list(profile.evidence),
        tmux=tmux_vars,
        pane_tail=pane_tail,
        recent_deliveries=[DeliveryRecord(**row) for row in recent],
    )


@router.get("/runtimes", response_model=list[RuntimeInfo])
async def list_runtimes():
    """Supported runtimes and whether their binary is installed."""
    return [
        RuntimeInfo(id=k, display_name=RUNTIME_DISPLAY_NAMES[k], available=runtime_available(k))
        for k in RUNTIME_COMMANDS
    ]


async def _start(
    request: Request,
    spec: AgentSpec,
    req: AgentStartRequest,
    store: AgentStore,
    tmux_svc: TmuxService,
) -> AgentStartResponse:
    config: BackboneConfig = request.app.state.config
    runtime = req.runtime or spec.runtime
    model = req.model if req.model is not None else spec.model

    # A runtime/model override at start becomes the agent's recorded setting,
    # so the next bare `agent start NAME` reuses it.
    changes: dict = {}
    if req.runtime and req.runtime != spec.runtime:
        changes["runtime"] = req.runtime
    if req.model is not None and req.model != spec.model:
        changes["model"] = req.model
    if changes:
        spec = await store.update(spec.name, **changes)

    if runtime not in RUNTIME_COMMANDS:
        raise HTTPException(status_code=400, detail=f"Unknown runtime: {runtime}")
    if not runtime_available(runtime):
        raise HTTPException(status_code=400, detail=f"Runtime '{runtime}' binary not found")
    if not spec.path.is_dir():
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {spec.path}")

    result = await start_agent(
        spec,
        config,
        runtime=runtime,
        model=model,
        resume=req.resume,
        db=request.app.state.db,
        wait=req.wait,
    )
    response = AgentStartResponse(
        ok=result.ok and result.ready != "exited",
        session=spec.name,
        name=spec.name,
        working_directory=str(spec.path),
        runtime=runtime,
        model=model,
        repo=spec.repo,
        already_existed=result.already_running,
        ready=result.ready,
        evidence=list(result.evidence),
    )
    if result.ok and not result.already_running:
        await store.touch_started(spec.name)
        await _broadcast(request, config, tmux_svc)
    return response


@router.post("/agents/start", response_model=AgentStartResponse)
async def start_agent_discover(
    request: Request,
    body: AgentStartRequest,
    store: AgentStore = Depends(get_agent_store),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Start an agent from a directory (discovering it) or by name."""
    directory = body.dir
    if directory:
        spec = await store.discover(
            directory, name=body.name, runtime=body.runtime, model=body.model
        )
        if body.watch:
            spec = AgentSpec(
                **{**spec.__dict__, "watches": tuple(dict.fromkeys([*spec.watches, *body.watch]))}
            )
        spec = await store.register(spec)
    elif body.name:
        spec = store.agents.get(body.name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown agent '{body.name}' — pass dir")
        if body.watch:
            for repo in body.watch:
                spec = await store.watch(body.name, repo)
    else:
        raise HTTPException(status_code=400, detail="name or dir is required")
    return await _start(request, spec, body, store, tmux_svc)


@router.post("/agents/{session}/start", response_model=AgentStartResponse)
async def start_known_agent(
    request: Request,
    session: str,
    body: AgentStartRequest | None = None,
    store: AgentStore = Depends(get_agent_store),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Start a known agent by name (``dir`` in the body registers it first)."""
    req = body or AgentStartRequest()
    directory = req.dir
    if directory:
        spec = await store.register(
            await store.discover(directory, name=session, runtime=req.runtime, model=req.model)
        )
    else:
        spec = store.agents.get(session)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail=f"'{session}' is not a known agent — pass dir to register it",
            )
    return await _start(request, spec, req, store, tmux_svc)


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


_APPROVE_STATUS = {"not_waiting": 409, "unsupported": 400, "offline": 404, "failed": 502}


@router.post("/agents/{name}/approve", response_model=AgentApproveResponse)
async def approve_agent_prompt(
    request: Request,
    name: str,
    body: AgentApproveRequest | None = None,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Answer the permission prompt a registered agent's runtime is showing.

    Sends the runtime's affirmative key(s) only while the dialog is on
    screen — a stale ``waiting_for_human`` state or an idle prompt is a 409,
    never a keystroke. Every approval is recorded as an ``approval`` event.
    """
    if not config.security.allow_remote_approval:
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote approval is disabled. Run "
                "`backbone config set security.allow_remote_approval true` to enable."
            ),
        )
    spec = registered_agent_or_404(config, name)
    approved_by = (body.from_entity if body else "") or "api"
    outcome, evidence = await approve_agent(name, runtime=spec.runtime)
    if outcome != "approved":
        raise HTTPException(
            status_code=_APPROVE_STATUS.get(outcome, 500),
            detail={"outcome": outcome, "evidence": evidence},
        )
    dialog = next((ln for ln in evidence[1:] if ln), "")
    event_id = await db.record_event(
        delivery_id=f"approval:{uuid.uuid4().hex}",
        source="backbone",
        event_type="approval",
        sender=approved_by,
        summary=f"{approved_by} approved a {spec.runtime} permission prompt on {name}: {dialog}",
    )
    if event_id is not None:
        await db.mark_event_processed(event_id, "approved")
    log.info("Permission prompt on '%s' approved by %s", name, approved_by)
    await _broadcast(request, config, tmux_svc)
    return AgentApproveResponse(
        ok=True, session=name, outcome=outcome, evidence=evidence, approved_by=approved_by
    )


@router.patch("/agents/{name}", response_model=AgentConfigResponse)
async def update_agent(
    name: str,
    body: AgentUpdateRequest,
    store: AgentStore = Depends(get_agent_store),
):
    """Change an agent's recorded settings (dir, runtime, model, repo, tags, env, description)."""
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        spec = await store.update(name, **changes)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{name}'") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AgentConfigResponse.from_spec(spec)


@router.post("/agents/{name}/watch", response_model=AgentConfigResponse)
async def watch_repo(name: str, body: WatchRequest, store: AgentStore = Depends(get_agent_store)):
    """Make an agent watch a repository (informational notifications + ``for:`` routing)."""
    try:
        spec = await store.watch(name, body.repo)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{name}'") from None
    return AgentConfigResponse.from_spec(spec)


@router.post("/agents/{name}/unwatch", response_model=AgentConfigResponse)
async def unwatch_repo(name: str, body: WatchRequest, store: AgentStore = Depends(get_agent_store)):
    await store.unwatch(name, body.repo)
    spec = store.agents.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{name}'")
    return AgentConfigResponse.from_spec(spec)


@router.delete("/agents/{name}")
async def forget_agent(
    name: str,
    store: AgentStore = Depends(get_agent_store),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Forget an agent (its session must be stopped first)."""
    if await tmux_svc.session_exists(name):
        raise HTTPException(status_code=409, detail=f"'{name}' is running — stop it first")
    removed = await store.forget(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{name}'")
    return {"ok": True, "name": name}


@router.get("/sessions", response_model=list[str])
async def get_sessions(tmux_svc: TmuxService = Depends(get_tmux_service)):
    """All active tmux sessions."""
    return await tmux_svc.list_sessions()


@router.get("/sessions/{name}/terminal")
async def get_terminal_output(
    name: str,
    lines: int = Query(default=50, ge=1, le=500),
    config: BackboneConfig = Depends(get_config),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Recent terminal output from a registered agent's session."""
    registered_agent_or_404(config, name)
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
):
    """Push an agent's state from outside — for runtimes the backbone ships no hook for.

    Writes the same state file the hooks write, so routing, the monitor and
    ``agent inspect`` all see it (the database mirror follows on the next
    read). Only registered agents have a state file.
    """
    registered_agent_or_404(config, session)
    record = {
        "state": body.state,
        "reason": body.reason,
        "issue": body.issue,
        "repo": body.repo,
        "ts": body.ts or time.time(),
        "plan_file": body.plan_file,
        "plan_title": body.plan_title,
    }
    write_state_file(config.state_dir, session, record)
    await _broadcast(request, config, getattr(request.app.state, "tmux_service", None))
    return {"ok": True, "session": session}
