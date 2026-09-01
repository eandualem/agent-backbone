"""Inbound event feed — what arrived (webhook/poll) and what the backbone did with it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from agent_backbone.api.deps import get_db
from agent_backbone.api.models import EventRecord, ListEnvelope
from agent_backbone.services.database import BackboneDB

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events", response_model=ListEnvelope[EventRecord])
async def list_events(
    repo: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: BackboneDB = Depends(get_db),
):
    """Most recent inbound events, newest first."""
    rows = await db.query_events(repo=repo, limit=limit)
    items = [EventRecord(**row) for row in rows]
    return ListEnvelope(items=items, total=len(items))
