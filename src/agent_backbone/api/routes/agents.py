"""Agent endpoints — discover, start, stop, inspect, edit."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_backbone.api.deps import (
    get_agent_store,
    get_config,
    get_db,
    get_feed,
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
from agent_backbone.api.session_updates import SessionFeed
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import (
    AgentStore,
    agent_state,
    approve_agent,
    read_state_file,
    record_answer,
    write_state_file,
)
from agent_backbone.services.agents.operations import StartRequest, resolve_agent, start_resolved
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing import get_session_intelligence
from agent_backbone.services.runtimes import RUNTIMES, sanitize_pane_content
from agent_backbone.services.terminal import (
    SESSION_FORMAT_STR,
    capture_pane,
    list_sessions,
    query_format_vars,
    session_exists,
    stop_session,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["agents"])


@router.get("/agents", response_model=ListEnvelope[EnrichedAgent])
async def list_agents(feed: SessionFeed = Depends(get_feed)):
    """Known agents (plus other live tmux sessions) with live state."""
    agents = await feed.snapshot()
    return ListEnvelope(items=agents, total=len(agents))


@router.get("/agents/{session}/state", response_model=AgentStateDetail)
async def get_agent_state_endpoint(session: str, config: BackboneConfig = Depends(get_config)):
    """Reconciled state for one agent."""
    snapshot = await agent_state(config, session)
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
):
    """Everything the backbone knows about an agent, with the evidence behind it."""
    spec = config.agents.get(name)
    online = await session_exists(name)
    profile = await get_session_intelligence(name, config)

    tmux_vars: dict = {}
    pane_tail: list[str] = []
    if online:
        try:
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
        recent = await db.deliveries.query(session_name=name, limit=10)
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
        session_id=profile.session_id,
        last_message=profile.last_message,
        detail=profile.detail,
        evidence=list(profile.evidence),
        tmux=tmux_vars,
        pane_tail=pane_tail,
        recent_deliveries=[DeliveryRecord(**row) for row in recent],
    )


@router.get("/runtimes", response_model=list[RuntimeInfo])
async def list_runtimes():
    """Supported runtimes and whether their binary is installed."""
    return [
        RuntimeInfo(
            id=rt.id, display_name=rt.display_name, available=rt.available(), models=list(rt.models)
        )
        for rt in RUNTIMES.values()
    ]


async def _start(
    request: Request, req: StartRequest, store: AgentStore, feed: SessionFeed
) -> AgentStartResponse:
    try:
        spec = await resolve_agent(store, req)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"'{exc.args[0]}' is not a known agent — pass dir to register it",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = await start_resolved(
            store, request.app.state.config, spec, req, db=request.app.state.db
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.ok and not result.already_running:
        await feed.refresh_and_emit()
    return AgentStartResponse(
        ok=result.ok and result.ready != "exited",
        session=spec.name,
        name=spec.name,
        working_directory=str(spec.path),
        runtime=req.runtime or spec.runtime,
        model=req.model if req.model is not None else spec.model,
        repo=spec.repo,
        already_existed=result.already_running,
        ready=result.ready,
        evidence=list(result.evidence),
    )


def _request(body: AgentStartRequest, *, name: str | None = None) -> StartRequest:
    return StartRequest(
        name=name or body.name,
        directory=body.dir,
        runtime=body.runtime,
        model=body.model,
        resume=body.resume,
        watch=tuple(body.watch),
        wait=body.wait,
    )


@router.post("/agents/start", response_model=AgentStartResponse)
async def start_agent_discover(
    request: Request,
    body: AgentStartRequest,
    store: AgentStore = Depends(get_agent_store),
    feed: SessionFeed = Depends(get_feed),
):
    """Start an agent from a directory (discovering it) or by name."""
    return await _start(request, _request(body), store, feed)


@router.post("/agents/{session}/start", response_model=AgentStartResponse)
async def start_known_agent(
    request: Request,
    session: str,
    body: AgentStartRequest | None = None,
    store: AgentStore = Depends(get_agent_store),
    feed: SessionFeed = Depends(get_feed),
):
    """Start a known agent by name (``dir`` in the body registers it first)."""
    return await _start(request, _request(body or AgentStartRequest(), name=session), store, feed)


@router.post("/agents/{session}/stop", response_model=AgentStopResponse)
async def stop_agent(
    session: str,
    config: BackboneConfig = Depends(get_config),
    feed: SessionFeed = Depends(get_feed),
):
    """Stop an agent tmux session."""
    if session == config.backbone.session_name:
        raise HTTPException(status_code=400, detail="Refusing to stop the backbone's own session")
    ok = await stop_session(session)
    if ok:
        await feed.refresh_and_emit()
    return AgentStopResponse(ok=ok, session=session)


_APPROVE_STATUS = {"not_waiting": 409, "unsupported": 400, "offline": 404, "failed": 502}


@router.post("/agents/{name}/approve", response_model=AgentApproveResponse)
async def approve_agent_prompt(
    name: str,
    body: AgentApproveRequest | None = None,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    feed: SessionFeed = Depends(get_feed),
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
    await record_answer(
        db, agent=name, runtime=spec.runtime, verb="approved", by=approved_by, evidence=evidence
    )
    await feed.refresh_and_emit()
    return AgentApproveResponse(
        ok=True, session=name, outcome=outcome, evidence=evidence, approved_by=approved_by
    )


@router.patch("/agents/{name}", response_model=AgentConfigResponse)
async def update_agent(
    name: str,
    body: AgentUpdateRequest,
    store: AgentStore = Depends(get_agent_store),
):
    """Change an agent's recorded settings (dir, runtime, model, repo, tags,
    env, description, always_on)."""
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
async def forget_agent(name: str, store: AgentStore = Depends(get_agent_store)):
    """Forget an agent (its session must be stopped first)."""
    if await session_exists(name):
        raise HTTPException(status_code=409, detail=f"'{name}' is running — stop it first")
    removed = await store.forget(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{name}'")
    return {"ok": True, "name": name}


@router.get("/sessions", response_model=list[str])
async def get_sessions():
    """All active tmux sessions."""
    return await list_sessions()


@router.get("/sessions/{name}/terminal")
async def get_terminal_output(
    name: str,
    lines: int = Query(default=50, ge=1, le=500),
    config: BackboneConfig = Depends(get_config),
):
    """Recent terminal output from a registered agent's session."""
    registered_agent_or_404(config, name)
    output = await capture_pane(name, lines=lines)
    if not output and not await session_exists(name):
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    return {"session": name, "lines": lines, "content": output}


@router.post("/agents/{session}/state")
async def post_agent_state(
    session: str,
    body: StateUpdateRequest,
    config: BackboneConfig = Depends(get_config),
    feed: SessionFeed = Depends(get_feed),
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
    await feed.refresh_and_emit()
    return {"ok": True, "session": session}
