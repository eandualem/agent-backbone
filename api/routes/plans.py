"""Plan management endpoints — view and approve pending plans."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_config
from api.models import ListEnvelope, PlanDetail
from src.agent_state import AgentState, get_agent_state, read_state_file
from src.config import BackboneConfig
from src.tmux import list_sessions, send_keys, session_exists

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["plans"])


@router.get("/plans", response_model=ListEnvelope[PlanDetail])
async def list_pending_plans(config: BackboneConfig = Depends(get_config)):
    """List all agents with plans awaiting approval."""
    plans: list[PlanDetail] = []

    # Check named entities
    all_sessions = list(config.registry.sessions_map.values())

    # Also check active coding agents
    active = await list_sessions()
    named = set(config.registry.sessions_map.values())
    all_sessions.extend(s for s in active if s not in named)

    for session in all_sessions:
        snapshot = await get_agent_state(
            config.agent_state.state_path,
            session,
            config.agent_state.stale_threshold_seconds,
        )
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
async def get_plan_detail(session: str, config: BackboneConfig = Depends(get_config)):
    """Get plan details including file content for a specific session."""
    snapshot = read_state_file(config.agent_state.state_path, session)
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
async def approve_plan(session: str):
    """Approve a pending plan by sending Shift+Tab (Escape + [Z) to the session."""
    if not await session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    # Send Escape first, then [Z (Shift+Tab sequence)
    await send_keys(session, "Escape")
    ok = await send_keys(session, "[Z")
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send approval keys")
    return {"ok": True, "session": session, "action": "plan_approved"}
