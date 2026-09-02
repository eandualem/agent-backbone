"""Delivery history and stats endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from agent_backbone.api.deps import get_db
from agent_backbone.api.models import DeliveryRecord, DeliveryStats, ListEnvelope
from agent_backbone.models import BLOCKED_OUTCOMES, SUCCESS_OUTCOMES, DeliveryOutcome
from agent_backbone.services.database import BackboneDB

router = APIRouter(prefix="/api", tags=["deliveries"])


@router.get("/deliveries", response_model=ListEnvelope[DeliveryRecord])
async def list_deliveries(
    issue_number: int | None = Query(default=None),
    repo: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    target_entity: str | None = Query(default=None),
    session: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: BackboneDB = Depends(get_db),
):
    """Query delivery records with optional filters."""
    rows = await db.deliveries.query(
        issue_number=issue_number,
        target_entity=target_entity,
        session_name=session,
        outcome=outcome,
        limit=limit,
        repo=repo,
        kind=kind,
    )
    items = [DeliveryRecord(**row) for row in rows]
    return ListEnvelope(items=items, total=len(items))


@router.get("/deliveries/failed", response_model=ListEnvelope[DeliveryRecord])
async def list_failed_deliveries(
    limit: int = Query(default=50, ge=1, le=500),
    db: BackboneDB = Depends(get_db),
):
    """Get deliveries with failed outcomes (offline, delivery_failed, deferred)."""
    rows = await db.deliveries.failed(limit=limit)
    items = [DeliveryRecord(**row) for row in rows]
    return ListEnvelope(items=items, total=len(items))


@router.get("/deliveries/stats", response_model=DeliveryStats)
async def get_delivery_stats(db: BackboneDB = Depends(get_db)):
    """Aggregate delivery statistics by outcome."""
    rows = await db.deliveries.stats()

    stats = DeliveryStats()
    for row in rows:
        outcome = row["outcome"]
        count = row["cnt"]
        stats.total += count
        if outcome in SUCCESS_OUTCOMES:
            stats.delivered += count
        elif outcome == DeliveryOutcome.OFFLINE:
            stats.offline += count
        elif outcome == DeliveryOutcome.DELIVERY_FAILED:
            stats.failed += count
        elif outcome in BLOCKED_OUTCOMES:
            stats.deferred += count
    return stats
