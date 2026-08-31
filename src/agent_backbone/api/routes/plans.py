"""Plan management endpoints — view and (optionally) act on pending plans.

Approving/rejecting a plan injects keystrokes into the agent's terminal, so
those endpoints are disabled unless ``[security] allow_remote_plan_control``
is set in backbone.toml.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, get_state_service, get_tmux_service
from agent_backbone.api.models import (
    ListEnvelope,
    PlanDetail,
    PlanRejectRequest,
    PlanRespondRequest,
)
from agent_backbone.api.session_updates import listable_sessions
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import AgentState, StateService
from agent_backbone.services.terminal import TmuxService, send_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["plans"])


def _require_plan_control(config: BackboneConfig) -> None:
    if not config.security.allow_remote_plan_control:
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote plan control is disabled. Set "
                "[security] allow_remote_plan_control = true in backbone.toml to enable."
            ),
        )


@router.get("/plans", response_model=ListEnvelope[PlanDetail])
async def list_pending_plans(
    config: BackboneConfig = Depends(get_config),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """List all agents with plans awaiting approval."""
    plans: list[PlanDetail] = []
    active = set(await tmux_svc.list_sessions())

    for session in listable_sessions(config, active):
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
    snapshot = state_svc.read_state(session)
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
    config: BackboneConfig = Depends(get_config),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Approve a pending plan by sending Shift+Tab (Escape + [Z) to the session."""
    _require_plan_control(config)
    if not await tmux_svc.session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    await tmux_svc.send_keys(session, "Escape")
    ok = await tmux_svc.send_keys(session, "[Z")
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send approval keys")
    return {"ok": True, "session": session, "action": "plan_approved"}


@router.post("/plans/{session}/reject")
async def reject_plan(
    session: str,
    body: PlanRejectRequest,
    config: BackboneConfig = Depends(get_config),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Reject a pending plan: exit plan mode and deliver the feedback."""
    _require_plan_control(config)
    snapshot = state_svc.read_state(session)
    if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
        raise HTTPException(
            status_code=409, detail=f"Session '{session}' is not in plan_waiting state"
        )

    if not await tmux_svc.session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    await tmux_svc.send_keys(session, "Escape")
    await send_message(session, f"[via:backbone] Plan rejected: {body.feedback}")

    log.info("Plan rejected for %s: %s", session, body.feedback[:80])
    return {"ok": True, "session": session, "action": "plan_rejected"}


@router.post("/plans/{session}/respond")
async def respond_to_plan(
    session: str,
    body: PlanRespondRequest,
    config: BackboneConfig = Depends(get_config),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Send input to a plan-waiting session (option selection or free text)."""
    _require_plan_control(config)
    snapshot = state_svc.read_state(session)
    if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
        raise HTTPException(
            status_code=409, detail=f"Session '{session}' is not in plan_waiting state"
        )

    if not await tmux_svc.session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    await send_message(session, body.input)

    log.info("Plan response sent to %s: %s", session, body.input[:80])
    return {"ok": True, "session": session, "action": "plan_response_sent"}
