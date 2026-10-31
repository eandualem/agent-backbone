"""Operator reads of captured communication; never claims or delivers inbox work."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.api.auth import require_operator
from agent_backbone.api.deps import get_db, get_feed
from agent_backbone.api.interaction_models import (
    AttemptPage,
    EventPage,
    Interaction,
    InteractionPage,
    StateSpanPage,
    Summary,
)
from agent_backbone.services.database import format_iso

router = APIRouter(
    prefix="/api/interactions", tags=["interactions"], dependencies=[Depends(require_operator)]
)


def window(since, until):
    try:
        dates = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in (since, until)]
        if any(t.tzinfo is None or t.utcoffset() is None for t in dates) or dates[0] >= dates[1]:
            raise ValueError
        return [format_iso(t) for t in dates]
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "Use timezone-aware since < until") from exc


@router.get("", response_model=InteractionPage)
async def history(
    agent_id: str = Query(min_length=32, max_length=32),
    since: str = Query(max_length=100),
    until: str = Query(max_length=100),
    direction: str = Query(default="both", pattern="^(both|incoming|outgoing)$"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=4096),
    snapshot: str | None = Query(default=None, max_length=4096),
    db=Depends(get_db),
):
    since, until = window(since, until)
    try:
        return await db.interactions.query(
            agent_id=agent_id,
            since=since,
            until=until,
            direction=direction,
            limit=limit,
            cursor=cursor,
            snapshot=snapshot,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/summary", response_model=Summary)
async def summary(
    since: str = Query(max_length=100),
    until: str = Query(max_length=100),
    agent_id: str | None = Query(default=None, min_length=32, max_length=32),
    snapshot: str | None = Query(default=None, max_length=4096),
    db=Depends(get_db),
    feed=Depends(get_feed),
):
    since, until = window(since, until)
    try:
        result = await db.interactions.summary(
            since=since, until=until, agent_id=agent_id, snapshot=snapshot
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    states = {a.name: a for a in await feed.snapshot()}
    for item in result["items"]:
        if not item["agent"]["retired"] and (state := states.get(item["agent"]["name"])):
            item["state"] = str(state.state)
            item["reason"] = state.reason
    return result


@router.get("/state-spans", response_model=StateSpanPage)
async def state_spans(
    agent_id: str | None = Query(default=None, min_length=32, max_length=32),
    since: str = Query(max_length=100),
    until: str = Query(max_length=100),
    limit: int = Query(default=200, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=4096),
    snapshot: str | None = Query(default=None, max_length=4096),
    db=Depends(get_db),
):
    since, until = window(since, until)
    try:
        return await db.interactions.state_spans(
            agent_id=agent_id,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
            snapshot=snapshot,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{interaction_id}", response_model=Interaction)
async def detail(interaction_id: str, db=Depends(get_db)):
    result = await db.interactions.detail(interaction_id)
    if result is None:
        raise HTTPException(404, "Interaction not found")
    return result


async def page(interaction_id, kind, cursor, limit, db):
    try:
        result = await db.interactions.subpage(interaction_id, kind, cursor, limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "Interaction not found")
    return result


@router.get("/{interaction_id}/attempts", response_model=AttemptPage)
async def attempts(
    interaction_id: str,
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=50, ge=1, le=200),
    db=Depends(get_db),
):
    return await page(interaction_id, "attempts", cursor, limit, db)


@router.get("/{interaction_id}/events", response_model=EventPage)
async def events(
    interaction_id: str,
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=50, ge=1, le=200),
    db=Depends(get_db),
):
    return await page(interaction_id, "events", cursor, limit, db)
