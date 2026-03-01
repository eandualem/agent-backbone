"""Heartbeat management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from agent_backbone.services.monitoring import MonitoringService
from agent_backbone.services.persistence import BackboneDB
from api.deps import get_db, get_monitoring_service
from api.models import HeartbeatRecord, ListEnvelope

router = APIRouter(prefix="/api", tags=["heartbeats"])


@router.get("/heartbeats/schedules")
async def get_heartbeat_schedules(
    monitoring_svc: MonitoringService = Depends(get_monitoring_service),
):
    """Get all heartbeat schedules."""
    return monitoring_svc.load_schedules()


@router.put("/heartbeats/schedules/{agent}")
async def update_heartbeat_schedule(
    agent: str,
    body: dict,
    monitoring_svc: MonitoringService = Depends(get_monitoring_service),
):
    """Update heartbeat schedule for a specific agent."""
    schedules = monitoring_svc.load_schedules()
    schedules[agent] = body
    monitoring_svc.save_schedules(schedules)
    return {"ok": True, "agent": agent}


@router.get("/heartbeats/history", response_model=ListEnvelope[HeartbeatRecord])
async def get_heartbeat_history(
    agent: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: BackboneDB = Depends(get_db),
):
    """Query heartbeat delivery history."""
    rows = await db.query_heartbeats(agent=agent, outcome=outcome, limit=limit)
    items = [HeartbeatRecord(**row) for row in rows]
    return ListEnvelope(items=items, total=len(items))
