"""Heartbeat management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from agent_backbone.config import BackboneConfig
from agent_backbone.services.persistence import BackboneDB
from api.deps import get_config, get_db
from api.models import HeartbeatRecord, ListEnvelope

router = APIRouter(prefix="/api", tags=["heartbeats"])


@router.get("/heartbeats/schedules")
async def get_heartbeat_schedules(config: BackboneConfig = Depends(get_config)):
    """Get all heartbeat schedules."""
    from agent_backbone.services.monitoring import load_schedules

    schedules = load_schedules(config.heartbeat.schedule_path)
    return schedules


@router.put("/heartbeats/schedules/{agent}")
async def update_heartbeat_schedule(
    agent: str,
    body: dict,
    config: BackboneConfig = Depends(get_config),
):
    """Update heartbeat schedule for a specific agent."""
    from agent_backbone.services.monitoring import load_schedules, save_schedules

    schedules = load_schedules(config.heartbeat.schedule_path)
    schedules[agent] = body
    save_schedules(schedules, config.heartbeat.schedule_path)
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
