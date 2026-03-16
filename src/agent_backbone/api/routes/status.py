"""System status and health endpoints."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends

from agent_backbone.api.deps import (
    get_config,
    get_db,
    get_github,
    get_state_service,
    get_tmux_service,
)
from agent_backbone.api.models import EnrichedAgent, ServiceHealth, SystemDigest
from agent_backbone.api.routes.agents import (
    _build_enriched_agent,
    _listable_registry_sessions,
    _reserved_agent_sessions,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.infrastructure._processes import read_pid
from agent_backbone.services.terminal import TmuxService, session_exists

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status", response_model=SystemDigest)
async def get_system_status(
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    gh: GitHubClient = Depends(get_github),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """System-wide status digest: sessions, agents, deliveries."""
    active = await tmux_svc.list_sessions()
    active_set = set(active)

    agents: list[EnrichedAgent] = []
    registry_sessions = _listable_registry_sessions(config)
    for entity, session in registry_sessions.items():
        agent = await _build_enriched_agent(
            session,
            entity,
            config,
            active_set,
            state_svc,
            agent_type="named_entity",
        )
        agents.append(agent)
    named_sessions = _reserved_agent_sessions(config)
    service_sessions = config.entities.service_sessions
    for session in active:
        if session not in named_sessions and session not in service_sessions:
            agent = await _build_enriched_agent(
                session,
                session,
                config,
                active_set,
                state_svc,
                agent_type="coding_agent",
            )
            agents.append(agent)

    failed_rows = await db.get_failed_deliveries(limit=1000)

    try:
        open_issues = await gh.list_issues(state="open")
        pending_issues = len(open_issues)
    except Exception:
        log.warning("Failed to fetch pending issues from GitHub")
        pending_issues = None

    return SystemDigest(
        active_sessions=active,
        agent_count=len(agents),
        pending_issues=pending_issues,
        failed_deliveries=len(failed_rows),
        agents=agents,
    )


@router.get("/status/services", response_model=ServiceHealth)
async def get_service_health(
    db: BackboneDB = Depends(get_db),
):
    """Check health of backbone services (gateway, Prefect, database)."""
    health = ServiceHealth(gateway="up")

    # Prefect server health
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:4200/api/health", timeout=3)
            health.prefect_server = "up" if resp.status_code == 200 else "degraded"
    except Exception:
        health.prefect_server = (
            "degraded" if await session_exists("prefect") or read_pid("prefect") else "down"
        )

    if read_pid("worker") is not None:
        health.prefect_worker = "up"
    elif await session_exists("backbone-worker"):
        health.prefect_worker = "degraded"
    else:
        health.prefect_worker = "down"

    # Database health
    try:
        if await db.check_connection():
            health.database = "up"
        else:
            health.database = "down"
    except Exception:
        health.database = "down"

    return health


@router.get("/config/entities")
async def get_entity_config(config: BackboneConfig = Depends(get_config)):
    """Return entity configuration (non-secret)."""
    return {
        "sessions": config.registry.sessions_map,
        "coding_repos": sorted(config.registry.repo_names),
        "skip": sorted(config.entities.skip),
        "fallback": config.entities.fallback,
    }
