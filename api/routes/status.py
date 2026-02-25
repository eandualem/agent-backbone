"""System status and health endpoints."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends

from api.deps import get_config, get_db, get_github
from api.models import EnrichedAgent, ServiceHealth, SystemDigest
from api.routes.agents import _build_enriched_agent
from src.config import BackboneConfig
from src.github import GitHubClient
from src.persistence import BackboneDB
from src.tmux import list_sessions

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status", response_model=SystemDigest)
async def get_system_status(
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    gh: GitHubClient = Depends(get_github),
):
    """System-wide status digest: sessions, agents, deliveries."""
    active = await list_sessions()

    agents: list[EnrichedAgent] = []
    for entity, session in config.registry.sessions_map.items():
        agent = await _build_enriched_agent(session, entity, config, agent_type="named_entity")
        agents.append(agent)
    named_sessions = set(config.registry.sessions_map.values())
    service_sessions = config.entities.service_sessions
    for session in active:
        if session not in named_sessions and session not in service_sessions:
            agent = await _build_enriched_agent(session, session, config, agent_type="coding_agent")
            agents.append(agent)

    failed_rows = await db.get_failed_deliveries(limit=1000)

    try:
        open_issues = await gh.list_issues(state="open")
        pending_issues = len(open_issues)
    except Exception:
        log.warning("Failed to fetch pending issues from GitHub")
        pending_issues = 0

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
        health.prefect_server = "down"

    # Database health
    try:
        cursor = await db.conn.execute("SELECT 1")
        await cursor.fetchone()
        health.database = "up"
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
