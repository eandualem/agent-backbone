"""Analytics API routes — read-only aggregation over agent_activity data.

Endpoints:
  GET /api/analytics/agents/{session}/overview
  GET /api/analytics/agents/{session}/events
  GET /api/analytics/agents/{session}/tools
  GET /api/analytics/agents/{session}/errors
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from agent_backbone.api.deps import get_db
from agent_backbone.api.models import (
    AnalyticsErrorsResponse,
    AnalyticsEventsResponse,
    AnalyticsOverviewResponse,
    AnalyticsToolsResponse,
)
from agent_backbone.services.analytics import AnalyticsService
from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Module-level singleton — stateless, no lifecycle needed
_analytics = AnalyticsService()


@router.get(
    "/agents/{session}/overview",
    response_model=AnalyticsOverviewResponse,
)
async def agent_overview(
    session: str,
    db: BackboneDB = Depends(get_db),
    since: float | None = Query(None, description="Unix epoch lower bound"),
    until: float | None = Query(None, description="Unix epoch upper bound"),
    include_swarm_workers: bool = Query(
        True,
        description="Include swarm worker sessions",
    ),
    swarm_id: str | None = Query(None, description="Filter to specific swarm"),
    worker_name: str | None = Query(
        None,
        description="Filter to specific worker name",
    ),
    worker_role: str | None = Query(
        None,
        description="Filter to specific worker role",
    ),
):
    """Aggregated overview: tokens, costs, errors, tools, duration."""
    return await _analytics.get_overview(
        db,
        session,
        since=since,
        until=until,
        include_swarm_workers=include_swarm_workers,
        swarm_id=swarm_id,
        worker_name=worker_name,
        worker_role=worker_role,
    )


@router.get(
    "/agents/{session}/events",
    response_model=AnalyticsEventsResponse,
)
async def agent_events(
    session: str,
    db: BackboneDB = Depends(get_db),
    since: float | None = Query(None),
    until: float | None = Query(None),
    include_swarm_workers: bool = Query(True),
    swarm_id: str | None = Query(None),
    worker_name: str | None = Query(None),
    worker_role: str | None = Query(None),
    event: str | None = Query(
        None,
        description="Filter by event type",
    ),
    runtime: str | None = Query(None),
    model: str | None = Query(None),
    source_kind: str | None = Query(None),
    source_ref: str | None = Query(None),
    trace_id: str | None = Query(None),
    parent_trace_id: str | None = Query(None),
    cursor_ts: str | None = Query(
        None,
        description="Cursor: timestamp from previous page",
    ),
    cursor_id: int | None = Query(
        None,
        description="Cursor: id from previous page",
    ),
    limit: int = Query(100, ge=1, le=1000),
):
    """Paginated event feed with stable-column filters."""
    return await _analytics.get_events(
        db,
        session,
        since=since,
        until=until,
        include_swarm_workers=include_swarm_workers,
        swarm_id=swarm_id,
        worker_name=worker_name,
        worker_role=worker_role,
        event=event,
        runtime=runtime,
        model=model,
        source_kind=source_kind,
        source_ref=source_ref,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
        limit=limit,
    )


@router.get(
    "/agents/{session}/tools",
    response_model=AnalyticsToolsResponse,
)
async def agent_tools(
    session: str,
    db: BackboneDB = Depends(get_db),
    since: float | None = Query(None),
    until: float | None = Query(None),
    include_swarm_workers: bool = Query(True),
    swarm_id: str | None = Query(None),
    worker_name: str | None = Query(None),
    worker_role: str | None = Query(None),
):
    """Grouped tool usage statistics."""
    return await _analytics.get_tools(
        db,
        session,
        since=since,
        until=until,
        include_swarm_workers=include_swarm_workers,
        swarm_id=swarm_id,
        worker_name=worker_name,
        worker_role=worker_role,
    )


@router.get(
    "/agents/{session}/errors",
    response_model=AnalyticsErrorsResponse,
)
async def agent_errors(
    session: str,
    db: BackboneDB = Depends(get_db),
    since: float | None = Query(None),
    until: float | None = Query(None),
    include_swarm_workers: bool = Query(True),
    swarm_id: str | None = Query(None),
    worker_name: str | None = Query(None),
    worker_role: str | None = Query(None),
):
    """Error summary and error feed."""
    return await _analytics.get_errors(
        db,
        session,
        since=since,
        until=until,
        include_swarm_workers=include_swarm_workers,
        swarm_id=swarm_id,
        worker_name=worker_name,
        worker_role=worker_role,
    )
