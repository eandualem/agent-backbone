"""Agent & session management endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_config
from api.models import AgentStateDetail, EnrichedAgent, ListEnvelope
from src.agent_state import get_agent_state
from src.config import BackboneConfig
from src.tmux import (
    capture_pane,
    list_sessions,
    list_sessions_rich,
    send_message,
    session_exists,
    start_session,
    stop_session,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["agents"])

# Named entity display info
_ENTITY_DISPLAY = {
    "feynman": {"display_name": "Feynman", "role": "Orchestration Optimizer"},
    "ike": {"display_name": "Ike", "role": "Core Orchestrator"},
    "leo": {"display_name": "Leo", "role": "Strategy Co-Architect"},
    "ada": {"display_name": "Ada", "role": "Spec Agent"},
    "brunel": {"display_name": "Brunel", "role": "Infrastructure Agent"},
}


async def _build_enriched_agent(
    session: str,
    entity: str,
    config: BackboneConfig,
    tmux_info: dict | None = None,
) -> EnrichedAgent:
    """Build an EnrichedAgent from session name and entity."""
    online = await session_exists(session)
    snapshot = await get_agent_state(
        config.agent_state.state_path,
        session,
        config.agent_state.stale_threshold_seconds,
    )
    display = _ENTITY_DISPLAY.get(entity, {})

    tmux_created = None
    tmux_attached = False
    tmux_windows = 0
    if tmux_info:
        created_ts = tmux_info.get("created", 0)
        if created_ts:
            tmux_created = datetime.fromtimestamp(created_ts, tz=UTC).isoformat()
        tmux_attached = tmux_info.get("attached", False)
        tmux_windows = tmux_info.get("windows", 0)

    return EnrichedAgent(
        session=session,
        entity=entity,
        display_name=display.get("display_name", session),
        role=display.get("role", "Coding Agent"),
        state=snapshot.state.value,
        current_issue=snapshot.current_issue,
        online=online,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
        tmux_created=tmux_created,
        tmux_attached=tmux_attached,
        tmux_windows=tmux_windows,
    )


@router.get("/agents", response_model=ListEnvelope[EnrichedAgent])
async def list_agents(config: BackboneConfig = Depends(get_config)):
    """List all agents (named entities + discovered coding agents) with live state."""
    agents: list[EnrichedAgent] = []

    # Fetch rich session info once
    rich_sessions = await list_sessions_rich()
    tmux_lookup = {s["name"]: s for s in rich_sessions}

    # Named entities
    for entity, session in config.entities.sessions.items():
        agent = await _build_enriched_agent(session, entity, config, tmux_lookup.get(session))
        agents.append(agent)

    # Discover coding agents from tmux sessions
    active = await list_sessions()
    named_sessions = set(config.entities.sessions.values())
    for session in active:
        if session not in named_sessions:
            agent = await _build_enriched_agent(session, session, config, tmux_lookup.get(session))
            agents.append(agent)

    return ListEnvelope(items=agents, total=len(agents))


@router.get("/agents/{session}/state", response_model=AgentStateDetail)
async def get_agent_state_endpoint(session: str, config: BackboneConfig = Depends(get_config)):
    """Get detailed state for a specific agent session."""
    snapshot = await get_agent_state(
        config.agent_state.state_path,
        session,
        config.agent_state.stale_threshold_seconds,
    )
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


@router.post("/agents/{session}/message")
async def send_agent_message(session: str, body: dict):
    """Send a message to an agent's tmux session."""
    message = body.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    ok = await send_message(session, message)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found or send failed")
    return {"ok": True, "session": session}


@router.post("/agents/{session}/start")
async def start_agent_session(session: str):
    """Start a new tmux session for an agent."""
    ok = await start_session(session)
    return {"ok": ok, "session": session}


@router.post("/agents/{session}/stop")
async def stop_agent_session(session: str):
    """Stop an agent's tmux session."""
    ok = await stop_session(session)
    return {"ok": ok, "session": session}


@router.get("/sessions", response_model=list[str])
async def get_sessions():
    """List all active tmux sessions."""
    return await list_sessions()


@router.get("/sessions/{name}/terminal")
async def get_terminal_output(name: str, lines: int = Query(default=50, ge=1, le=500)):
    """Capture recent terminal output from a tmux session."""
    output = await capture_pane(name, lines=lines)
    if not output and not await session_exists(name):
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    return {"session": name, "lines": lines, "content": output}
