"""Swarm registry endpoints."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.data_streams import notify_stream
from agent_backbone.api.deps import get_config, get_db
from agent_backbone.api.models import (
    ListEnvelope,
    SwarmAssignmentCreateRequest,
    SwarmAssignmentDispatchResponse,
    SwarmAssignmentResponse,
    SwarmBroadcastRequest,
    SwarmBroadcastResponse,
    SwarmCreateRequest,
    SwarmCreateResponse,
    SwarmDetailResponse,
    SwarmMessageLogEntry,
    SwarmMessageRequest,
    SwarmPhase,
    SwarmPhaseHistoryResponse,
    SwarmPhaseUpdateRequest,
    SwarmProgress,
    SwarmSummaryResponse,
    SwarmWorkerCompleteRequest,
    SwarmWorkerResponse,
    SwarmWorkerStatusUpdateRequest,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.messaging import deliver_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["swarms"])


def _to_swarm_worker_response(worker: dict) -> SwarmWorkerResponse:
    return SwarmWorkerResponse(
        worker_id=worker["worker_id"],
        swarm_id=worker["swarm_id"],
        name=worker["name"],
        role=worker["role"],
        branch=worker["branch"],
        worktree_path=worker["worktree_path"],
        session=worker["session"],
        status=worker["status"],
        pr_number=worker.get("pr_number"),
        summary=worker.get("summary"),
        failure_reason=worker.get("failure_reason"),
        completed_at=worker.get("completed_at"),
        created_at=worker["created_at"],
        updated_at=worker["updated_at"],
    )


def _to_phase_history_response(entry: dict) -> SwarmPhaseHistoryResponse:
    return SwarmPhaseHistoryResponse(
        history_id=entry["history_id"],
        swarm_id=entry["swarm_id"],
        from_phase=entry.get("from_phase"),
        to_phase=entry["to_phase"],
        timestamp=entry["timestamp"],
        triggered_by=entry["triggered_by"],
    )


def _to_swarm_assignment_response(assignment: dict) -> SwarmAssignmentResponse:
    return SwarmAssignmentResponse(
        assignment_id=assignment["assignment_id"],
        swarm_id=assignment["swarm_id"],
        worker_name=assignment["worker_name"],
        assigned_by=assignment["assigned_by"],
        summary=assignment["summary"],
        file_paths=assignment.get("file_paths", []),
        status=assignment["status"],
        created_at=assignment["created_at"],
        completed_at=assignment.get("completed_at"),
    )


def _to_swarm_summary_response(swarm: dict) -> SwarmSummaryResponse:
    return SwarmSummaryResponse(
        swarm_id=swarm["swarm_id"],
        repo=swarm["repo"],
        task_id=swarm.get("task_id"),
        coding_agent_session=swarm["coding_agent_session"],
        phase=swarm["phase"],
        created_at=swarm["created_at"],
        completed_at=swarm.get("completed_at"),
        worker_count=swarm.get("worker_count", 0),
        progress=SwarmProgress.model_validate(swarm.get("progress", {})),
    )


def _to_swarm_detail_response(swarm: dict) -> SwarmDetailResponse:
    summary = _to_swarm_summary_response(swarm)
    workers = [_to_swarm_worker_response(worker) for worker in swarm.get("workers", [])]
    workers_by_role = {
        role: [_to_swarm_worker_response(worker) for worker in role_workers]
        for role, role_workers in swarm.get("workers_by_role", {}).items()
    }
    assignments = [
        _to_swarm_assignment_response(assignment) for assignment in swarm.get("assignments", [])
    ]
    return SwarmDetailResponse(
        **summary.model_dump(),
        workers=workers,
        workers_by_role=workers_by_role,
        assignments=assignments,
        phase_history=[
            _to_phase_history_response(entry) for entry in swarm.get("phase_history", [])
        ],
    )


def _to_swarm_message_log_entry(entry: dict) -> SwarmMessageLogEntry:
    return SwarmMessageLogEntry(
        message_id=entry["message_id"],
        swarm_id=entry["swarm_id"],
        target_kind=entry["target_kind"],
        target_role=entry.get("target_role"),
        target_worker_name=entry.get("target_worker_name"),
        from_entity=entry["from_entity"],
        message=entry["message"],
        delivered=entry["delivered"],
        failed=entry["failed"],
        total=entry["total"],
        created_at=entry["created_at"],
    )


def _format_swarm_message(
    swarm: dict,
    from_entity: str,
    message: str,
    *,
    target_label: str,
) -> str:
    parts = [f"[via:swarm swarm:{swarm['swarm_id']} from:{from_entity}]"]
    parts.append(f"Repo: {swarm['repo']}")
    if swarm.get("task_id"):
        parts.append(f"Task: {swarm['task_id']}")
    parts.append(f"Lead session: {swarm['coding_agent_session']}")
    parts.append(f"Target: {target_label}")
    parts.append("")
    parts.append(message)
    return "\n".join(parts)


def _format_assignment_message(summary: str, file_paths: list[str]) -> str:
    lines = [f"Assignment: {summary}"]
    if file_paths:
        lines.append("")
        lines.append("Owned files:")
        lines.extend(f"- {path}" for path in file_paths)
    return "\n".join(lines)


async def _deliver_to_workers(
    workers: list[dict],
    envelope: str,
    *,
    config: BackboneConfig,
    db: BackboneDB,
) -> tuple[int, int]:
    async def _deliver(worker: dict) -> str:
        result = await deliver_message(
            worker["session"],
            envelope,
            config,
        )
        if result != "delivered":
            log.warning(
                "Swarm message delivery failed for '%s' (%s): %s",
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
                "Swarm message exception for '%s': %s",
                workers[index]["name"],
                result,
            )
            continue
        if result == "delivered":
            delivered += 1
    total = len(workers)
    failed = total - delivered
    return delivered, failed


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
    lead_count = sum(1 for worker in body.workers if worker.role == "lead")
    if lead_count > 1:
        raise HTTPException(status_code=400, detail="Collaborative swarms can only have one lead")

    swarm_id = await db.create_swarm(
        repo=body.repo,
        task_id=body.task_id,
        coding_agent_session=body.coding_agent_session,
        workers=[worker.model_dump() for worker in body.workers],
    )
    await notify_stream("swarms")
    return SwarmCreateResponse(swarm_id=swarm_id)


@router.get("/swarms", response_model=ListEnvelope[SwarmSummaryResponse])
async def list_swarms(
    repo: str | None = None,
    phase: SwarmPhase | None = None,
    db: BackboneDB = Depends(get_db),
):
    """List visible swarms, with optional repo/phase filters."""
    swarms = await db.list_swarms(repo=repo, phase=phase)
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


@router.post("/swarms/{swarm_id}/phase", response_model=SwarmDetailResponse)
async def update_swarm_phase(
    swarm_id: str,
    body: SwarmPhaseUpdateRequest,
    db: BackboneDB = Depends(get_db),
):
    """Update the swarm phase through the legal lifecycle graph."""
    try:
        swarm = await db.update_swarm_phase(swarm_id, body.phase)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")
    await notify_stream("swarms")
    return _to_swarm_detail_response(swarm)


@router.post("/swarms/{swarm_id}/workers/{worker_name}/status", response_model=SwarmDetailResponse)
async def update_worker_status(
    swarm_id: str,
    worker_name: str,
    body: SwarmWorkerStatusUpdateRequest,
    db: BackboneDB = Depends(get_db),
):
    """Update a worker status and return the refreshed swarm."""
    try:
        swarm = await db.update_swarm_worker_status(
            swarm_id,
            worker_name,
            body.status,
            pr_number=body.pr_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm worker not found")
    await notify_stream("swarms")
    return _to_swarm_detail_response(swarm)


@router.post(
    "/swarms/{swarm_id}/workers/{worker_name}/complete",
    response_model=SwarmDetailResponse,
)
async def complete_worker(
    swarm_id: str,
    worker_name: str,
    body: SwarmWorkerCompleteRequest,
    db: BackboneDB = Depends(get_db),
):
    """Mark a worker done/failed with its completion summary."""
    try:
        swarm = await db.complete_swarm_worker(
            swarm_id,
            worker_name,
            body.status,
            body.summary,
            pr_number=body.pr_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm worker not found")
    await notify_stream("swarms")
    return _to_swarm_detail_response(swarm)


@router.delete("/swarms/{swarm_id}", response_model=SwarmDetailResponse)
async def complete_swarm(
    swarm_id: str,
    db: BackboneDB = Depends(get_db),
):
    """Mark a merged swarm cleaned up."""
    try:
        swarm = await db.complete_swarm(swarm_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")
    await notify_stream("swarms")
    return _to_swarm_detail_response(swarm)


@router.post(
    "/swarms/{swarm_id}/assignments",
    response_model=SwarmAssignmentDispatchResponse,
)
async def create_swarm_assignment(
    swarm_id: str,
    body: SwarmAssignmentCreateRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Create and dispatch one lead-owned assignment for a worker."""
    swarm = await db.get_swarm(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")

    try:
        assignment = await db.create_swarm_assignment(
            swarm_id,
            body.worker_name,
            assigned_by=body.from_entity,
            summary=body.summary,
            file_paths=body.file_paths,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if assignment is None:
        raise HTTPException(status_code=404, detail="Swarm worker not found")

    target_workers = [
        worker for worker in swarm.get("workers", []) if worker["name"] == body.worker_name
    ]
    if not target_workers:
        raise HTTPException(status_code=404, detail="Swarm worker not found")

    message = _format_assignment_message(body.summary, body.file_paths)
    envelope = _format_swarm_message(
        swarm,
        body.from_entity,
        message,
        target_label=f"worker:{body.worker_name}",
    )
    delivered, failed = await _deliver_to_workers(
        target_workers,
        envelope,
        config=config,
        db=db,
    )
    message_log = await db.record_swarm_message(
        swarm_id,
        target_kind="worker",
        target_worker_name=body.worker_name,
        from_entity=body.from_entity,
        message=message,
        delivered=delivered,
        failed=failed,
        total=len(target_workers),
    )
    return SwarmAssignmentDispatchResponse(
        ok=failed == 0,
        assignment_id=assignment["assignment_id"],
        message_id=message_log["message_id"],
        delivered=delivered,
        failed=failed,
        total=len(target_workers),
        assignment=_to_swarm_assignment_response(assignment),
    )


@router.get(
    "/swarms/{swarm_id}/assignments",
    response_model=ListEnvelope[SwarmAssignmentResponse],
)
async def list_swarm_assignments(
    swarm_id: str,
    db: BackboneDB = Depends(get_db),
):
    """List all persisted assignments for one swarm."""
    swarm = await db.get_swarm(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")

    items = [
        _to_swarm_assignment_response(assignment)
        for assignment in await db.list_swarm_assignments(swarm_id)
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/swarms/{swarm_id}/broadcast", response_model=SwarmBroadcastResponse)
async def broadcast_swarm_message(
    swarm_id: str,
    body: SwarmBroadcastRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Broadcast a lead message to all worker sessions in a swarm."""
    swarm = await db.get_swarm(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")

    workers = swarm.get("workers", [])
    envelope = _format_swarm_message(
        swarm,
        body.from_entity,
        body.message,
        target_label="all workers",
    )
    delivered, failed = await _deliver_to_workers(
        workers,
        envelope,
        config=config,
        db=db,
    )
    message_log = await db.record_swarm_message(
        swarm_id,
        target_kind="broadcast",
        from_entity=body.from_entity,
        message=body.message,
        delivered=delivered,
        failed=failed,
        total=len(workers),
    )
    return SwarmBroadcastResponse(
        ok=failed == 0,
        message_id=message_log["message_id"],
        delivered=delivered,
        failed=failed,
        total=len(workers),
    )


@router.post("/swarms/{swarm_id}/message", response_model=SwarmBroadcastResponse)
async def send_swarm_message(
    swarm_id: str,
    body: SwarmMessageRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Send a role-targeted or worker-targeted message within a swarm."""
    swarm = await db.get_swarm(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")

    workers = swarm.get("workers", [])
    if body.role is not None:
        target_workers = [worker for worker in workers if worker["role"] == body.role]
        if not target_workers:
            raise HTTPException(
                status_code=404,
                detail=f"Swarm has no workers with role '{body.role}'",
            )
        target_label = f"role:{body.role}"
        target_kind = "role"
        target_role = body.role
        target_worker_name = None
    else:
        target_workers = [worker for worker in workers if worker["name"] == body.worker_name]
        if not target_workers:
            raise HTTPException(
                status_code=404,
                detail=f"Swarm has no worker named '{body.worker_name}'",
            )
        target_label = f"worker:{body.worker_name}"
        target_kind = "worker"
        target_role = None
        target_worker_name = body.worker_name

    envelope = _format_swarm_message(
        swarm,
        body.from_entity,
        body.message,
        target_label=target_label,
    )
    delivered, failed = await _deliver_to_workers(
        target_workers,
        envelope,
        config=config,
        db=db,
    )
    message_log = await db.record_swarm_message(
        swarm_id,
        target_kind=target_kind,
        target_role=target_role,
        target_worker_name=target_worker_name,
        from_entity=body.from_entity,
        message=body.message,
        delivered=delivered,
        failed=failed,
        total=len(target_workers),
    )
    return SwarmBroadcastResponse(
        ok=failed == 0,
        message_id=message_log["message_id"],
        delivered=delivered,
        failed=failed,
        total=len(target_workers),
    )


@router.get("/swarms/{swarm_id}/messages", response_model=ListEnvelope[SwarmMessageLogEntry])
async def list_swarm_messages(
    swarm_id: str,
    db: BackboneDB = Depends(get_db),
):
    """List the persisted swarm message log in timestamp order."""
    swarm = await db.get_swarm(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")

    items = [_to_swarm_message_log_entry(entry) for entry in await db.list_swarm_messages(swarm_id)]
    return ListEnvelope(items=items, total=len(items))
