"""Swarm registry endpoints."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, get_db, get_delivery_service
from agent_backbone.api.models import (
    ListEnvelope,
    SwarmBroadcastRequest,
    SwarmBroadcastResponse,
    SwarmCreateRequest,
    SwarmCreateResponse,
    SwarmDetailResponse,
    SwarmProgress,
    SwarmStatus,
    SwarmSummaryResponse,
    SwarmWorkerResponse,
    SwarmWorkerStatusUpdateRequest,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing import DeliveryService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["swarms"])


def _to_swarm_worker_response(worker: dict) -> SwarmWorkerResponse:
    return SwarmWorkerResponse(
        worker_id=worker["worker_id"],
        swarm_id=worker["swarm_id"],
        name=worker["name"],
        branch=worker["branch"],
        worktree_path=worker["worktree_path"],
        session=worker["session"],
        status=worker["status"],
        pr_number=worker.get("pr_number"),
        created_at=worker["created_at"],
        updated_at=worker["updated_at"],
    )


def _to_swarm_summary_response(swarm: dict) -> SwarmSummaryResponse:
    return SwarmSummaryResponse(
        swarm_id=swarm["swarm_id"],
        repo=swarm["repo"],
        task_id=swarm.get("task_id"),
        coding_agent_session=swarm["coding_agent_session"],
        status=swarm["status"],
        created_at=swarm["created_at"],
        completed_at=swarm.get("completed_at"),
        worker_count=swarm.get("worker_count", 0),
        progress=SwarmProgress.model_validate(swarm.get("progress", {})),
    )


def _to_swarm_detail_response(swarm: dict) -> SwarmDetailResponse:
    summary = _to_swarm_summary_response(swarm)
    return SwarmDetailResponse(
        **summary.model_dump(),
        workers=[_to_swarm_worker_response(worker) for worker in swarm.get("workers", [])],
    )


def _format_swarm_broadcast_message(swarm: dict, from_entity: str, message: str) -> str:
    parts = [f"[via:swarm swarm:{swarm['swarm_id']} from:{from_entity}]"]
    parts.append(f"Repo: {swarm['repo']}")
    if swarm.get("task_id"):
        parts.append(f"Task: {swarm['task_id']}")
    parts.append(f"Lead session: {swarm['coding_agent_session']}")
    parts.append("")
    parts.append(message)
    return "\n".join(parts)


@router.post("/swarms", response_model=SwarmCreateResponse, status_code=201)
async def create_swarm(
    body: SwarmCreateRequest,
    db: BackboneDB = Depends(get_db),
):
    """Register a new swarm and its workers."""
    if not body.workers:
        raise HTTPException(status_code=400, detail="Swarm must include at least one worker")
    worker_names = [worker.name for worker in body.workers]
    if len(set(worker_names)) != len(worker_names):
        raise HTTPException(status_code=400, detail="Worker names must be unique within a swarm")

    swarm_id = await db.create_swarm(
        repo=body.repo,
        task_id=body.task_id,
        coding_agent_session=body.coding_agent_session,
        workers=[worker.model_dump() for worker in body.workers],
    )
    return SwarmCreateResponse(swarm_id=swarm_id)


@router.get("/swarms", response_model=ListEnvelope[SwarmSummaryResponse])
async def list_swarms(
    repo: str | None = None,
    status: SwarmStatus | None = None,
    db: BackboneDB = Depends(get_db),
):
    """List unfinished swarms by default, with optional repo/status filters."""
    swarms = await db.list_swarms(repo=repo, status=status)
    items = [_to_swarm_summary_response(swarm) for swarm in swarms]
    return ListEnvelope(items=items, total=len(items))


@router.get("/swarms/{swarm_id}", response_model=SwarmDetailResponse)
async def get_swarm(
    swarm_id: str,
    db: BackboneDB = Depends(get_db),
):
    """Get full detail for one swarm."""
    swarm = await db.get_swarm(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")
    return _to_swarm_detail_response(swarm)


@router.post("/swarms/{swarm_id}/workers/{worker_name}/status", response_model=SwarmDetailResponse)
async def update_worker_status(
    swarm_id: str,
    worker_name: str,
    body: SwarmWorkerStatusUpdateRequest,
    db: BackboneDB = Depends(get_db),
):
    """Update a worker status and return the refreshed swarm."""
    swarm = await db.update_swarm_worker_status(
        swarm_id,
        worker_name,
        body.status,
        pr_number=body.pr_number,
    )
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm worker not found")
    return _to_swarm_detail_response(swarm)


@router.delete("/swarms/{swarm_id}", response_model=SwarmDetailResponse)
async def complete_swarm(
    swarm_id: str,
    db: BackboneDB = Depends(get_db),
):
    """Mark a swarm completed."""
    swarm = await db.complete_swarm(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")
    return _to_swarm_detail_response(swarm)


@router.post("/swarms/{swarm_id}/broadcast", response_model=SwarmBroadcastResponse)
async def broadcast_swarm_message(
    swarm_id: str,
    body: SwarmBroadcastRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Broadcast a lead message to all worker sessions in a swarm."""
    swarm = await db.get_swarm(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")

    workers = swarm.get("workers", [])
    envelope = _format_swarm_broadcast_message(swarm, body.from_entity, body.message)

    async def _deliver(worker: dict) -> str:
        result = await delivery_svc.safe_deliver(worker["session"], envelope, config)
        if result != "delivered":
            log.warning(
                "Swarm broadcast delivery failed for '%s' (%s): %s",
                worker["name"],
                worker["session"],
                result,
            )
        return result

    raw_results = await asyncio.gather(
        *[_deliver(worker) for worker in workers],
        return_exceptions=True,
    )

    delivered = 0
    for index, result in enumerate(raw_results):
        if isinstance(result, BaseException):
            log.warning(
                "Swarm broadcast exception for '%s': %s",
                workers[index]["name"],
                result,
            )
            continue
        if result == "delivered":
            delivered += 1
    total = len(workers)
    failed = total - delivered
    return SwarmBroadcastResponse(
        ok=failed == 0,
        delivered=delivered,
        failed=failed,
        total=total,
    )
