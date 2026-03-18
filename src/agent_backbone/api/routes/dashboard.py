"""Unified dashboard endpoint — single request for all dashboard data."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Depends

from agent_backbone.api.deps import (
    get_config,
    get_db,
    get_github,
    get_state_service,
    get_tmux_service,
)
from agent_backbone.api.models import (
    DashboardCounts,
    DashboardResponse,
    EnrichedAgent,
    ServiceHealth,
)
from agent_backbone.api.session_updates import build_session_snapshot
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.infrastructure._processes import read_pid
from agent_backbone.services.terminal import TmuxService, session_exists

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])

# TTL caches
_agents_cache: list[EnrichedAgent] = []
_agents_cache_ts: float = 0
_AGENTS_CACHE_TTL = 5.0

_issues_cache: int = 0
_issues_cache_ts: float = 0
_ISSUES_CACHE_TTL = 60.0


async def _fetch_agents(
    config: BackboneConfig,
    state_svc: StateService,
    tmux_svc: TmuxService,
) -> list[EnrichedAgent]:
    """Build the full agent list with TTL cache (5s)."""
    global _agents_cache, _agents_cache_ts  # noqa: PLW0603
    now = time.monotonic()
    if now - _agents_cache_ts < _AGENTS_CACHE_TTL and _agents_cache:
        return _agents_cache

    agents = await build_session_snapshot(config, state_svc, tmux_svc)
    _agents_cache = agents
    _agents_cache_ts = now
    return agents


async def _fetch_issues_pending(gh: GitHubClient) -> int:
    """Get count of open GitHub issues with TTL cache (60s)."""
    global _issues_cache, _issues_cache_ts  # noqa: PLW0603
    now = time.monotonic()
    if now - _issues_cache_ts < _ISSUES_CACHE_TTL:
        return _issues_cache

    try:
        open_issues = await gh.list_issues(state="open")
        count = len(open_issues)
    except Exception:
        log.warning("Failed to fetch pending issues from GitHub")
        count = 0

    _issues_cache = count
    _issues_cache_ts = now
    return count


async def _fetch_failed_deliveries(db: BackboneDB) -> int:
    """Get count of failed deliveries."""
    rows = await db.get_failed_deliveries(limit=1000)
    return len(rows)


async def _fetch_service_health(db: BackboneDB) -> ServiceHealth:
    """Check health of backbone services."""
    health = ServiceHealth(gateway="up")

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

    try:
        if await db.check_connection():
            health.database = "up"
        else:
            health.database = "down"
    except Exception:
        health.database = "down"

    return health


def _compute_counts(agents: list[EnrichedAgent]) -> DashboardCounts:
    """Derive aggregate counts from the agent list."""
    counts = DashboardCounts(total=len(agents))
    for agent in agents:
        if agent.state == "offline":
            counts.offline += 1
        elif agent.state == "idle":
            counts.idle += 1
            counts.active += 1
        elif agent.state in ("plan_waiting", "permission_waiting"):
            counts.plan_waiting += 1
            counts.active += 1
        elif agent.state in ("processing_issue", "busy", "blocked"):
            counts.busy += 1
            counts.active += 1
        else:
            # unknown or other — count as active if online
            if agent.online:
                counts.active += 1
    return counts


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    gh: GitHubClient = Depends(get_github),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Unified dashboard endpoint — returns all dashboard data in one request."""
    agents, issues_pending, failed_deliveries, services = await asyncio.gather(
        _fetch_agents(config, state_svc, tmux_svc),
        _fetch_issues_pending(gh),
        _fetch_failed_deliveries(db),
        _fetch_service_health(db),
    )

    counts = _compute_counts(agents)
    plans_pending = sum(1 for a in agents if a.state in ("plan_waiting", "permission_waiting"))

    return DashboardResponse(
        agents=agents,
        counts=counts,
        plans_pending=plans_pending,
        issues_pending=issues_pending,
        failed_deliveries=failed_deliveries,
        services=services,
    )
