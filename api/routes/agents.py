"""Agent & session management endpoints."""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.deps import get_config
from api.models import AgentStartRequest, AgentStateDetail, EnrichedAgent, ListEnvelope, RuntimeInfo
from src.agent_state import get_agent_state
from src.config import BackboneConfig
from src.tmux import (
    capture_pane,
    list_sessions,
    list_sessions_rich,
    resolve_agent_dir,
    send_message,
    session_exists,
    start_session,
    stop_session,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["agents"])

# Fallback directories for binaries not on system PATH
_FALLBACK_DIRS = [
    Path.home() / ".bun" / "bin",
    Path.home() / ".local" / "bin",
]


def _resolve_command(name: str | None) -> str | None:
    """Resolve a command name to an absolute path.

    Tries shutil.which() first (system PATH), then checks fallback
    directories. Returns the absolute path string, or None if unresolved.
    """
    if name is None:
        return None
    path = shutil.which(name)
    if path:
        return path
    for directory in _FALLBACK_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


# Runtime registry
_RUNTIMES = {
    "claude": {"display_name": "Claude Code", "command": "claude"},
    "gemini": {"display_name": "Gemini CLI", "command": "gemini"},
    "codex": {"display_name": "Codex", "command": "codex"},
    "shell": {"display_name": "Plain Shell", "command": None},
}

# Resolve commands to absolute paths at load time
for _rt_id, _rt_entry in _RUNTIMES.items():
    _rt_entry["resolved_path"] = _resolve_command(_rt_entry["command"])
    if _rt_entry["command"] and not _rt_entry["resolved_path"]:
        log.warning("Runtime '%s': binary '%s' not found", _rt_id, _rt_entry["command"])

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
    agent_type: str = "coding_agent",
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
        type=agent_type,
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
        agent = await _build_enriched_agent(
            session, entity, config, tmux_lookup.get(session), agent_type="named_entity"
        )
        agents.append(agent)

    # Discover coding agents from tmux sessions (exclude services)
    active = await list_sessions()
    named_sessions = set(config.entities.sessions.values())
    service_sessions = config.entities.service_sessions
    for session in active:
        if session not in named_sessions and session not in service_sessions:
            agent = await _build_enriched_agent(
                session, session, config, tmux_lookup.get(session), agent_type="coding_agent"
            )
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


@router.get("/runtimes", response_model=list[RuntimeInfo])
async def list_runtimes():
    """List available runtimes for agent sessions."""
    return [
        RuntimeInfo(
            id=k,
            display_name=v["display_name"],
            available=v["command"] is None or v["resolved_path"] is not None,
        )
        for k, v in _RUNTIMES.items()
    ]


@router.post("/agents/{session}/start")
async def start_agent_session(
    session: str,
    body: AgentStartRequest | None = Body(default=None),
):
    """Start a new tmux session for an agent.

    Optionally accepts a JSON body with working_directory and runtime.
    If working_directory is not provided, resolves from session name.
    If runtime is not provided, defaults to "claude".
    """
    runtime_id = (body.runtime if body else None) or "claude"
    if runtime_id not in _RUNTIMES:
        available = list(_RUNTIMES.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown runtime '{runtime_id}'. Available: {available}",
        )
    runtime_entry = _RUNTIMES[runtime_id]
    command = runtime_entry["command"]
    resolved = runtime_entry["resolved_path"]

    # Validate binary is available (shell has no command)
    if command is not None and resolved is None:
        raise HTTPException(
            status_code=400,
            detail=f"Runtime '{runtime_id}': binary '{command}' not found on this system",
        )

    working_dir = (body.working_directory if body else None) or resolve_agent_dir(session)
    ok = await start_session(
        session,
        working_dir=working_dir or None,
        command=resolved,
    )
    return {
        "ok": ok,
        "session": session,
        "working_directory": working_dir or None,
        "runtime": runtime_id,
    }


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
