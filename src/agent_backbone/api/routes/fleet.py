"""Fleet snapshots — save the running agents and resume their exact conversations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.api.deps import get_agent_store, get_config, get_db, get_feed
from agent_backbone.api.models import (
    FleetResumeRequest,
    FleetResumeRun,
    FleetSaveRequest,
    FleetSnapshotSummary,
    FleetSnapshotView,
)
from agent_backbone.api.session_updates import SessionFeed
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import AgentStore
from agent_backbone.services.agents.fleet import resume_fleet, save_counts, save_fleet, summary
from agent_backbone.services.database import BackboneDB

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


def _view(snapshot: dict) -> FleetSnapshotView:
    return FleetSnapshotView(**snapshot, counts=save_counts(snapshot["agents"]))


async def _snapshot(db: BackboneDB, snapshot_id: str) -> dict:
    if snapshot_id == "latest":
        snapshot = await db.fleet.latest()
    elif snapshot_id.isdigit():
        snapshot = await db.fleet.get(int(snapshot_id))
    else:
        raise HTTPException(status_code=400, detail="a snapshot id is a number or 'latest'")
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"no fleet snapshot '{snapshot_id}'")
    return snapshot


@router.post("/snapshots", response_model=FleetSnapshotView, status_code=201)
async def save_snapshot(
    body: FleetSaveRequest,
    config: BackboneConfig = Depends(get_config),
    store: AgentStore = Depends(get_agent_store),
    db: BackboneDB = Depends(get_db),
    feed: SessionFeed = Depends(get_feed),
):
    """Save every running agent and its conversation; with ``stop``, then stop them."""
    snapshot = await save_fleet(
        store,
        config,
        db,
        stop=body.stop,
        force=body.force,
        note=body.note,
        from_entity=body.from_entity,
    )
    if snapshot is None:
        raise HTTPException(status_code=409, detail="no running agents")
    if body.stop:
        await feed.refresh_and_emit()
    return _view(snapshot)


@router.get("/snapshots", response_model=list[FleetSnapshotSummary])
async def list_snapshots(limit: int = Query(20, ge=1, le=100), db: BackboneDB = Depends(get_db)):
    """Snapshots, newest first."""
    return [summary(snapshot) for snapshot in await db.fleet.recent(limit)]


@router.get("/snapshots/{snapshot_id}", response_model=FleetSnapshotView)
async def get_snapshot(snapshot_id: str, db: BackboneDB = Depends(get_db)):
    """One snapshot (a number or ``latest``) with every resume run of it."""
    return _view(await _snapshot(db, snapshot_id))


@router.post("/snapshots/{snapshot_id}/resume", response_model=FleetResumeRun)
async def resume_snapshot(
    snapshot_id: str,
    body: FleetResumeRequest | None = None,
    config: BackboneConfig = Depends(get_config),
    store: AgentStore = Depends(get_agent_store),
    db: BackboneDB = Depends(get_db),
    feed: SessionFeed = Depends(get_feed),
):
    """Start each saved agent on exactly its saved conversation; report each outcome."""
    body = body or FleetResumeRequest()
    snapshot = await _snapshot(db, snapshot_id)
    run = await resume_fleet(
        store, config, db, snapshot, from_entity=body.from_entity, wait=body.wait
    )
    await feed.refresh_and_emit()
    return run
