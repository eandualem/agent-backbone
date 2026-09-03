"""Plan management endpoints — view and (optionally) act on pending plans.

Approving or rejecting a plan sends keys to the agent's terminal, so those
endpoints are disabled unless the ``security.allow_remote_plan_control``
setting is on (``backbone config set security.allow_remote_plan_control true``).
The keys are the runtime's own (``Runtime.plan_approve_keys`` /
``plan_reject_keys``): a runtime without a plan mode the backbone can drive
answers 409 and nothing is typed. Feedback and responses are
``plan_response`` deliveries through ``safe_deliver`` — gated, recorded,
never queued.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, get_db, registered_agent_or_404
from agent_backbone.api.models import (
    ListEnvelope,
    PlanDetail,
    PlanRejectRequest,
    PlanRespondRequest,
)
from agent_backbone.api.session_updates import listable_sessions
from agent_backbone.config import BackboneConfig
from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.agents import agent_state, plan_control, read_plan, read_state_file
from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.terminal import list_sessions

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


def _require_plan_waiting(config: BackboneConfig, session: str) -> None:
    snapshot = read_state_file(config.state_dir, session)
    if not snapshot or not snapshot.is_plan_waiting:
        raise HTTPException(
            status_code=409, detail=f"Session '{session}' is not waiting for plan approval"
        )


async def _run_plan_control(config: BackboneConfig, session: str, action: str) -> list[str]:
    """Send the runtime's plan keys; translate every refusal into an HTTP status."""
    spec = config.agents.get(session)
    outcome, evidence = await plan_control(session, action, runtime=spec.runtime if spec else None)
    if outcome == "unsupported":
        raise HTTPException(
            status_code=409,
            detail=f"Plan control is not available for '{session}': {evidence[0]}",
        )
    if outcome == "offline":
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")
    if outcome == "failed":
        raise HTTPException(status_code=500, detail=f"Could not send the plan keys: {evidence[0]}")
    return evidence


async def _deliver_plan_response(config, db, session: str, message: str) -> DeliveryOutcome:
    """Type an answer into the plan prompt — only while it is on screen, never queued."""
    outcome = await safe_deliver(
        session,
        message,
        config,
        db=db,
        source="api-plans",
        delivery_kind="plan_response",
    )
    if outcome != DeliveryOutcome.DELIVERED:
        raise HTTPException(
            status_code=409,
            detail=f"Plan response not delivered to '{session}': {outcome.value}",
        )
    return outcome


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
    """Approve a pending plan with the agent's runtime's own keys."""
    _require_plan_control(config)
    registered_agent_or_404(config, session)
    _require_plan_waiting(config, session)
    evidence = await _run_plan_control(config, session, "approve")
    return {"ok": True, "session": session, "action": "plan_approved", "evidence": evidence}


@router.post("/plans/{session}/reject")
async def reject_plan(
    session: str,
    body: PlanRejectRequest,
    config: BackboneConfig = Depends(get_config),
    db=Depends(get_db),
):
    """Reject a pending plan: leave plan mode, then send the feedback as a message.

    Once plan mode is left the agent is back at an ordinary prompt, so the
    feedback is an ordinary direct message: enveloped, gated, queued if the
    agent is not ready yet.
    """
    _require_plan_control(config)
    registered_agent_or_404(config, session)
    _require_plan_waiting(config, session)
    await _run_plan_control(config, session, "reject")
    outcome = await safe_deliver(
        session,
        f"[via:backbone] Plan rejected: {body.feedback}",
        config,
        db=db,
        source="api-plans",
        delivery_kind="direct_message",
    )

    log.info("Plan rejected for %s: %s (%s)", session, body.feedback[:80], outcome)
    return {
        "ok": True,
        "session": session,
        "action": "plan_rejected",
        "feedback": outcome.value,
    }


@router.post("/plans/{session}/respond")
async def respond_to_plan(
    session: str,
    body: PlanRespondRequest,
    config: BackboneConfig = Depends(get_config),
    db=Depends(get_db),
):
    """Send input to a plan-waiting session (option selection or free text)."""
    _require_plan_control(config)
    registered_agent_or_404(config, session)
    _require_plan_waiting(config, session)
    await _deliver_plan_response(config, db, session, body.input)

    log.info("Plan response sent to %s: %s", session, body.input[:80])
    return {"ok": True, "session": session, "action": "plan_response_sent"}
