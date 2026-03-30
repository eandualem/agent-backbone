"""Plan management endpoints — view and approve pending plans."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.data_streams import notify_stream
from agent_backbone.api.deps import get_config, get_state_service, get_tmux_service
from agent_backbone.api.models import (
    ListEnvelope,
    PlanDetail,
    PlanRejectRequest,
    PlanRespondRequest,
)
from agent_backbone.services.agents import AgentState, StateService
from agent_backbone.services.terminal import TmuxService, send_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["plans"])


@router.get("/plans", response_model=ListEnvelope[PlanDetail])
async def list_pending_plans(
    config=Depends(get_config),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """List all agents with plans awaiting approval."""
    plans: list[PlanDetail] = []

    # Check named entities
    all_sessions = list(config.registry.sessions_map.values())

    # Also check active coding agents
    active = await tmux_svc.list_sessions()
    named = set(config.registry.sessions_map.values())
    all_sessions.extend(s for s in active if s not in named)

    for session in all_sessions:
        snapshot = await state_svc.get_state(session)
        if snapshot.state == AgentState.PLAN_WAITING:
            plans.append(
                PlanDetail(
                    session=session,
                    state=snapshot.state.value,
                    plan_file=snapshot.plan_file,
                    plan_title=snapshot.plan_title,
                )
            )

    return ListEnvelope(items=plans, total=len(plans))


@router.get("/plans/{session}", response_model=PlanDetail)
async def get_plan_detail(
    session: str,
    state_svc: StateService = Depends(get_state_service),
):
    """Get plan details including file content for a specific session."""
    snapshot = await state_svc.get_reported_state(session)
    if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
        raise HTTPException(status_code=404, detail=f"No pending plan for session '{session}'")

    content = None
    if snapshot.plan_file:
        plan_path = Path(snapshot.plan_file).expanduser()
        if plan_path.exists():
            try:
                content = plan_path.read_text()
            except OSError:
                content = None

    return PlanDetail(
        session=session,
        state=snapshot.state.value,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
        content=content,
    )


@router.post("/plans/{session}/approve")
async def approve_plan(
    session: str,
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Approve a pending plan by sending Shift+Tab (Escape + [Z) to the session."""
    if not await tmux_svc.session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    # Send Escape first, then [Z (Shift+Tab sequence)
    await tmux_svc.send_keys(session, "Escape")
    ok = await tmux_svc.send_keys(session, "[Z")
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send approval keys")
    await notify_stream("plans")
    await notify_stream("agents")
    return {"ok": True, "session": session, "action": "plan_approved"}


@router.post("/plans/{session}/reject")
async def reject_plan(
    session: str,
    body: PlanRejectRequest,
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Reject a pending plan by exiting plan mode and sending feedback.

    Sends Escape to exit plan mode, then delivers the feedback message
    so the agent sees why the plan was rejected.
    """
    snapshot = await state_svc.get_reported_state(session)
    if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
        raise HTTPException(
            status_code=409, detail=f"Session '{session}' is not in plan_waiting state"
        )

    if not await tmux_svc.session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    # Exit plan mode
    await tmux_svc.send_keys(session, "Escape")

    # Deliver feedback
    feedback = f"[Plan rejected] {body.feedback}"
    await send_message(session, feedback)

    log.info("Plan rejected for %s: %s", session, body.feedback[:80])
    return {"ok": True, "session": session, "action": "plan_rejected"}


@router.post("/plans/{session}/respond")
async def respond_to_plan(
    session: str,
    body: PlanRespondRequest,
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Send input to a plan-waiting session (option selection or free text).

    Delivers the literal input text to the session via send_message,
    which handles load-buffer/paste-buffer safely.
    """
    snapshot = await state_svc.get_reported_state(session)
    if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
        raise HTTPException(
            status_code=409, detail=f"Session '{session}' is not in plan_waiting state"
        )

    if not await tmux_svc.session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    await send_message(session, body.input)

    log.info("Plan response sent to %s: %s", session, body.input[:80])
    return {"ok": True, "session": session, "action": "plan_response_sent"}
