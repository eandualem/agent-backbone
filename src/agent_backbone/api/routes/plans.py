"""Plan management endpoints — view and approve pending plans."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, get_state_service, get_tmux_service
from agent_backbone.api.models import ListEnvelope, PlanDetail
from agent_backbone.services.agents import AgentState, StateService
from agent_backbone.services.terminal import TmuxService

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
    return {"ok": True, "session": session, "action": "plan_approved"}
