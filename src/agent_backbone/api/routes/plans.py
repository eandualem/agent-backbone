"""Plan management endpoints — view and (optionally) act on pending plans.

Approving/rejecting a plan injects keystrokes into the agent's terminal, so
those endpoints are disabled unless the ``security.allow_remote_plan_control``
setting is on (``backbone config set security.allow_remote_plan_control true``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, registered_agent_or_404
from agent_backbone.api.models import (
    ListEnvelope,
    PlanDetail,
    PlanRejectRequest,
    PlanRespondRequest,
)
from agent_backbone.api.session_updates import listable_sessions
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import agent_state, approve_plan, read_plan, read_state_file
from agent_backbone.services.runtimes import send_message
from agent_backbone.services.terminal import list_sessions, send_keys, session_exists

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["plans"])


def _require_plan_control(config: BackboneConfig) -> None:
    if not config.security.allow_remote_plan_control:
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote plan control is disabled. Run "
                "`backbone config set security.allow_remote_plan_control true` to enable."
            ),
        )


@router.get("/plans", response_model=ListEnvelope[PlanDetail])
async def list_pending_plans(config: BackboneConfig = Depends(get_config)):
    """List all agents with plans awaiting approval."""
    plans: list[PlanDetail] = []
    active = set(await list_sessions())

    for session in listable_sessions(config, active):
        snapshot = await agent_state(config, session)
        if snapshot.is_plan_waiting:
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
async def get_plan_detail(session: str, config: BackboneConfig = Depends(get_config)):
    """Get plan details including file content for a specific session."""
    snapshot = read_state_file(config.state_dir, session)
    if not snapshot or not snapshot.is_plan_waiting:
        raise HTTPException(status_code=404, detail=f"No pending plan for session '{session}'")

    return PlanDetail(
        session=session,
        state=snapshot.state.value,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
        content=read_plan(config.state_dir, snapshot),
    )


@router.post("/plans/{session}/approve")
async def approve_pending_plan(session: str, config: BackboneConfig = Depends(get_config)):
    """Approve a pending plan by sending Shift+Tab (Escape + [Z) to the session."""
    _require_plan_control(config)
    registered_agent_or_404(config, session)
    if not await session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    if not await approve_plan(session):
        raise HTTPException(status_code=500, detail="Failed to send approval keys")
    return {"ok": True, "session": session, "action": "plan_approved"}


@router.post("/plans/{session}/reject")
async def reject_plan(
    session: str,
    body: PlanRejectRequest,
    config: BackboneConfig = Depends(get_config),
):
    """Reject a pending plan: exit plan mode and deliver the feedback."""
    _require_plan_control(config)
    registered_agent_or_404(config, session)
    snapshot = read_state_file(config.state_dir, session)
    if not snapshot or not snapshot.is_plan_waiting:
        raise HTTPException(
            status_code=409, detail=f"Session '{session}' is not waiting for plan approval"
        )

    if not await session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    await send_keys(session, "Escape")
    await send_message(session, f"[via:backbone] Plan rejected: {body.feedback}")

    log.info("Plan rejected for %s: %s", session, body.feedback[:80])
    return {"ok": True, "session": session, "action": "plan_rejected"}


@router.post("/plans/{session}/respond")
async def respond_to_plan(
    session: str,
    body: PlanRespondRequest,
    config: BackboneConfig = Depends(get_config),
):
    """Send input to a plan-waiting session (option selection or free text)."""
    _require_plan_control(config)
    registered_agent_or_404(config, session)
    snapshot = read_state_file(config.state_dir, session)
    if not snapshot or not snapshot.is_plan_waiting:
        raise HTTPException(
            status_code=409, detail=f"Session '{session}' is not waiting for plan approval"
        )

    if not await session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    await send_message(session, body.input)

    log.info("Plan response sent to %s: %s", session, body.input[:80])
    return {"ok": True, "session": session, "action": "plan_response_sent"}
