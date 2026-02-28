"""Delivery history and stats endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from agent_backbone.services.persistence import BackboneDB
from api.deps import get_db
from api.models import DeliveryRecord, DeliveryStats, ListEnvelope

router = APIRouter(prefix="/api", tags=["deliveries"])


@router.get("/deliveries", response_model=ListEnvelope[DeliveryRecord])
async def list_deliveries(
    issue_number: int | None = Query(default=None),
    target_entity: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: BackboneDB = Depends(get_db),
):
    """Query delivery records with optional filters."""
    rows = await db.query_deliveries(
        issue_number=issue_number,
        target_entity=target_entity,
        outcome=outcome,
        limit=limit,
    )
    items = [DeliveryRecord(**row) for row in rows]
    return ListEnvelope(items=items, total=len(items))


@router.get("/deliveries/failed", response_model=ListEnvelope[DeliveryRecord])
async def list_failed_deliveries(
    limit: int = Query(default=50, ge=1, le=500),
    db: BackboneDB = Depends(get_db),
):
    """Get deliveries with failed outcomes (offline, delivery_failed, deferred)."""
    rows = await db.get_failed_deliveries(limit=limit)
    items = [DeliveryRecord(**row) for row in rows]
    return ListEnvelope(items=items, total=len(items))


@router.get("/deliveries/stats", response_model=DeliveryStats)
async def get_delivery_stats(db: BackboneDB = Depends(get_db)):
    """Aggregate delivery statistics by outcome."""
    rows = await db.get_delivery_stats()

    stats = DeliveryStats()
    for row in rows:
        outcome = row["outcome"]
        count = row["cnt"]
        stats.total += count
        if outcome == "delivered":
            stats.delivered += count
        elif outcome == "offline":
            stats.offline += count
        elif outcome == "deferred":
            stats.deferred += count
        elif outcome in ("delivery_failed", "failed"):
            stats.failed += count
        else:
            stats.total += 0  # already counted
    return stats
