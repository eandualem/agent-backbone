"""Unified dashboard endpoint — single request for all dashboard data."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends

from agent_backbone.api.deps import (
    get_config,
    get_db,
    get_issue_service,
    get_state_service,
    get_tmux_service,
)
from agent_backbone.api.models import (
    DashboardCounts,
    DashboardResponse,
    EnrichedAgent,
    ServiceHealth,
)
from agent_backbone.api.session_updates import build_session_snapshot, get_cached_session_snapshot
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.issues import IssueService
from agent_backbone.services.terminal import TmuxService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])

_issues_cache: int = 0
_issues_cache_ts: float = 0
_ISSUES_CACHE_TTL = 60.0


async def _fetch_agents(
    config: BackboneConfig,
    state_svc: StateService,
    tmux_svc: TmuxService,
) -> list[EnrichedAgent]:
    """Build the full agent list with the shared session snapshot cache."""
    return await get_cached_session_snapshot(
        lambda: build_session_snapshot(config, state_svc, tmux_svc)
    )


async def _fetch_issues_pending(issue_service: IssueService) -> int:
    """Get count of open issues from the canonical projection with TTL cache."""
    global _issues_cache, _issues_cache_ts  # noqa: PLW0603
    now = time.monotonic()
    if now - _issues_cache_ts < _ISSUES_CACHE_TTL:
        return _issues_cache

    try:
        count = await issue_service.count_open_issues()
    except Exception:
        log.warning("Failed to fetch pending issues from issue projection")
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
        elif agent.state == "plan_waiting":
            counts.plan_waiting += 1
            counts.active += 1
        elif agent.state == "permission_waiting":
            counts.permission_waiting += 1
            counts.active += 1
        elif agent.state == "sub_agent_waiting":
            counts.sub_agent_waiting += 1
            counts.active += 1
        elif agent.state == "starting":
            counts.starting += 1
            counts.active += 1
        elif agent.state == "busy":
            counts.busy += 1
            counts.active += 1
    return counts


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    issue_service: IssueService = Depends(get_issue_service),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Unified dashboard endpoint — returns all dashboard data in one request."""
    agents, issues_pending, failed_deliveries, services = await asyncio.gather(
        _fetch_agents(config, state_svc, tmux_svc),
        _fetch_issues_pending(issue_service),
        _fetch_failed_deliveries(db),
        _fetch_service_health(db),
    )

    counts = _compute_counts(agents)
    plans_pending = sum(1 for a in agents if a.state == "plan_waiting")

    return DashboardResponse(
        agents=agents,
        counts=counts,
        plans_pending=plans_pending,
        issues_pending=issues_pending,
        failed_deliveries=failed_deliveries,
        services=services,
    )
