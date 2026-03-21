"""Swarm registry repository."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_backbone.services.database.models import (
    SwarmAssignmentORM,
    SwarmMessageORM,
    SwarmORM,
    SwarmPhaseHistoryORM,
    SwarmWorkerORM,
)

_SWARM_PHASE_ORDER = (
    "created",
    "planning",
    "working",
    "validating",
    "pr_open",
    "awaiting_review",
    "merged",
    "cleaned_up",
    "failed",
    "discarded",
)
_SWARM_PHASE_INDEX = {phase: index for index, phase in enumerate(_SWARM_PHASE_ORDER)}
_SWARM_WORKER_ROLES = ("lead", "coder", "tester", "validator", "scout")
_NONTERMINAL_WORKER_STATUSES = {"pending", "started", "working", "pr_created"}
_TERMINAL_WORKER_STATUSES = {"done", "failed"}
_TERMINAL_ASSIGNMENT_STATUSES = {"completed", "superseded", "cancelled"}
_VISIBLE_SWARM_PHASES = {
    "created",
    "planning",
    "working",
    "validating",
    "pr_open",
    "awaiting_review",
    "merged",
    "failed",
}
_MANUAL_PHASE_TRANSITIONS = {
    "created": {"planning", "failed", "discarded"},
    "planning": {"working", "failed", "discarded"},
    "working": {"validating", "failed", "discarded"},
    "validating": {"working", "pr_open", "failed", "discarded"},
    "pr_open": {"working", "awaiting_review", "failed", "discarded"},
    "awaiting_review": {"working", "merged", "failed", "discarded"},
    "merged": {"cleaned_up"},
    "cleaned_up": set(),
    "failed": set(),
    "discarded": set(),
}
_COMPLETION_PHASES = {"merged", "cleaned_up", "failed", "discarded"}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _group_rows(rows: list[dict], key: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def _rows_to_dicts(result) -> list[dict]:
    return [dict(row._mapping) for row in result.fetchall()]


def _group_workers_by_role(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {role: [] for role in _SWARM_WORKER_ROLES}
    for row in rows:
        grouped.setdefault(row["role"], []).append(row)
    return grouped


def _build_progress(workers: list[dict]) -> dict:
    counts = {
        "pending": 0,
        "started": 0,
        "working": 0,
        "pr_created": 0,
        "done": 0,
        "failed": 0,
    }
    for worker in workers:
        status = worker["status"]
        if status in counts:
            counts[status] += 1
    total = len(workers)
    finished = counts["done"] + counts["failed"]
    percent = (finished / total * 100.0) if total else 0.0
    return {
        **counts,
        "total": total,
        "finished": finished,
        "percent": percent,
    }


def _decode_file_paths(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    paths: list[str] = []
    for value in parsed:
        if isinstance(value, str) and value:
            paths.append(value)
    return paths


def _encode_file_paths(file_paths: list[str]) -> str:
    return json.dumps(file_paths)


def _decode_assignment_row(row: dict) -> dict:
    return {
        **row,
        "file_paths": _decode_file_paths(row.get("file_paths")),
    }


def _lead_workers(workers: list[dict]) -> list[dict]:
    return [worker for worker in workers if worker["role"] == "lead"]


def _is_collaborative_swarm(workers: list[dict]) -> bool:
    return bool(_lead_workers(workers))


def _build_swarm_detail(
    swarm: dict,
    workers: list[dict],
    phase_history: list[dict],
    assignments: list[dict] | None = None,
) -> dict:
    ordered_workers = sorted(workers, key=lambda worker: (worker["created_at"], worker["name"]))
    workers_by_role = _group_workers_by_role(ordered_workers)
    return {
        **swarm,
        "worker_count": len(ordered_workers),
        "progress": _build_progress(ordered_workers),
        "workers": ordered_workers,
        "workers_by_role": workers_by_role,
        "assignments": assignments or [],
        "phase_history": phase_history,
    }


def _can_auto_promote_validating(phase: str, workers: list[dict]) -> bool:
    if not workers:
        return False
    if all(worker["status"] in _TERMINAL_WORKER_STATUSES for worker in workers):
        return _SWARM_PHASE_INDEX.get(phase, 0) < _SWARM_PHASE_INDEX["validating"]
    return False


def _phase_completed_at(current_completed_at: str | None, phase: str, now: str) -> str | None:
    if phase in _COMPLETION_PHASES:
        return current_completed_at or now
    return None


async def create_swarm(
    conn: AsyncConnection,
    repo: str,
    task_id: str | None,
    coding_agent_session: str,
    workers: list[dict],
) -> str:
    """Create a swarm and its workers. Returns the swarm ID."""
    now = _now_iso()
    swarm_id = str(uuid.uuid4())
    await conn.execute(
        insert(SwarmORM).values(
            swarm_id=swarm_id,
            repo=repo,
            task_id=task_id,
            coding_agent_session=coding_agent_session,
            phase="created",
            created_at=now,
            completed_at=None,
        )
    )
    await _insert_phase_history(
        conn,
        swarm_id=swarm_id,
        from_phase=None,
        to_phase="created",
        triggered_by=coding_agent_session,
        timestamp=now,
    )

    for worker in workers:
        await conn.execute(
            insert(SwarmWorkerORM).values(
                worker_id=str(uuid.uuid4()),
                swarm_id=swarm_id,
                name=worker["name"],
                role=worker["role"],
                branch=worker["branch"],
                worktree_path=worker["worktree_path"],
                session=worker["session"],
                status="pending",
                pr_number=None,
                summary=None,
                failure_reason=None,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
        )
    return swarm_id


async def list_swarms(
    conn: AsyncConnection,
    repo: str | None = None,
    phase: str | None = None,
) -> list[dict]:
    """List swarms with aggregated worker progress."""
    stmt = select(SwarmORM.__table__)
    if repo:
        stmt = stmt.where(SwarmORM.repo == repo)
    if phase:
        stmt = stmt.where(SwarmORM.phase == phase)
    else:
        stmt = stmt.where(SwarmORM.phase.in_(sorted(_VISIBLE_SWARM_PHASES)))

    result = await conn.execute(stmt.order_by(SwarmORM.created_at.desc()))
    swarms = _rows_to_dicts(result)
    if not swarms:
        return []

    swarm_ids = [swarm["swarm_id"] for swarm in swarms]
    worker_rows = await _fetch_workers_for_swarms(conn, swarm_ids)
    history_rows = await _fetch_phase_history_for_swarms(conn, swarm_ids)
    grouped_workers = _group_rows(worker_rows, "swarm_id")
    grouped_history = _group_rows(history_rows, "swarm_id")
    return [
        _build_swarm_detail(
            swarm,
            grouped_workers.get(swarm["swarm_id"], []),
            grouped_history.get(swarm["swarm_id"], []),
        )
        for swarm in swarms
    ]


async def get_swarm(
    conn: AsyncConnection,
    swarm_id: str,
) -> dict | None:
    """Get a single swarm with worker details."""
    swarm = await _fetch_swarm_row(conn, swarm_id)
    if swarm is None:
        return None

    workers = await _fetch_workers_for_swarms(conn, [swarm_id])
    phase_history = await _fetch_phase_history_for_swarms(conn, [swarm_id])
    assignments = await list_assignments(conn, swarm_id)
    return _build_swarm_detail(swarm, workers, phase_history, assignments)


async def update_worker_status(
    conn: AsyncConnection,
    swarm_id: str,
    worker_name: str,
    status: str,
    pr_number: int | None = None,
) -> dict | None:
    """Update a worker's non-terminal status."""
    if status not in _NONTERMINAL_WORKER_STATUSES:
        raise ValueError("Use the completion endpoint for done/failed worker states")

    if await _fetch_swarm_row(conn, swarm_id) is None:
        return None
    worker = await _fetch_worker_row(conn, swarm_id, worker_name)
    if worker is None:
        return None
    if worker["status"] in _TERMINAL_WORKER_STATUSES:
        raise ValueError("Cannot change status of a completed worker")

    workers = await _fetch_workers_for_swarms(conn, [swarm_id])
    if _is_collaborative_swarm(workers):
        if worker["role"] == "lead":
            if status in {"working", "pr_created"}:
                raise ValueError("Lead workers cannot enter implementation statuses")
        else:
            active_assignment = await _fetch_active_assignment_row(conn, swarm_id, worker_name)
            if active_assignment is None and status != "pending":
                raise ValueError("Worker must have an active assignment before starting work")

    now = _now_iso()
    values = {
        "status": status,
        "updated_at": now,
    }
    if pr_number is not None:
        values["pr_number"] = pr_number
    await conn.execute(
        update(SwarmWorkerORM)
        .where(SwarmWorkerORM.swarm_id == swarm_id, SwarmWorkerORM.name == worker_name)
        .values(**values)
    )

    await _auto_promote_validating_if_ready(
        conn,
        swarm_id,
        triggered_by=f"worker:{worker_name}",
    )
    return await get_swarm(conn, swarm_id)


async def complete_worker(
    conn: AsyncConnection,
    swarm_id: str,
    worker_name: str,
    status: str,
    summary: str,
    pr_number: int | None = None,
    *,
    failure_reason: str | None = None,
) -> dict | None:
    """Mark a worker done or failed."""
    if status not in _TERMINAL_WORKER_STATUSES:
        raise ValueError("Worker completion status must be done or failed")

    swarm = await _fetch_swarm_row(conn, swarm_id)
    if swarm is None:
        return None
    worker = await _fetch_worker_row(conn, swarm_id, worker_name)
    if worker is None:
        return None
    if worker["status"] in _TERMINAL_WORKER_STATUSES and worker["status"] != status:
        raise ValueError("Cannot change a terminal worker outcome")

    workers = await _fetch_workers_for_swarms(conn, [swarm_id])
    if _is_collaborative_swarm(workers) and worker["role"] != "lead":
        active_assignment = await _fetch_active_assignment_row(conn, swarm_id, worker_name)
        if active_assignment is None:
            raise ValueError("Worker must have an active assignment before reporting completion")

    now = _now_iso()
    values = {
        "status": status,
        "summary": summary,
        "failure_reason": failure_reason if status == "failed" else None,
        "completed_at": worker.get("completed_at") or now,
        "updated_at": now,
    }
    if pr_number is not None:
        values["pr_number"] = pr_number
    await conn.execute(
        update(SwarmWorkerORM)
        .where(SwarmWorkerORM.swarm_id == swarm_id, SwarmWorkerORM.name == worker_name)
        .values(**values)
    )
    await _close_active_assignments_for_worker(
        conn,
        swarm_id,
        worker_name,
        assignment_status="completed" if status == "done" else "cancelled",
        completed_at=now,
    )

    await _auto_promote_validating_if_ready(
        conn,
        swarm_id,
        triggered_by=f"worker:{worker_name}",
    )
    return await get_swarm(conn, swarm_id)


async def update_swarm_phase(
    conn: AsyncConnection,
    swarm_id: str,
    phase: str,
) -> dict | None:
    """Update swarm phase with legal transition validation."""
    swarm = await _fetch_swarm_row(conn, swarm_id)
    if swarm is None:
        return None
    await _set_swarm_phase(
        conn,
        swarm,
        phase,
        triggered_by=swarm["coding_agent_session"],
        validate_transition=True,
    )
    return await get_swarm(conn, swarm_id)


async def complete_swarm(
    conn: AsyncConnection,
    swarm_id: str,
) -> dict | None:
    """Mark a merged swarm cleaned up."""
    swarm = await _fetch_swarm_row(conn, swarm_id)
    if swarm is None:
        return None
    await _set_swarm_phase(
        conn,
        swarm,
        "cleaned_up",
        triggered_by="system:cleanup",
        validate_transition=True,
    )
    return await get_swarm(conn, swarm_id)


async def create_assignment(
    conn: AsyncConnection,
    swarm_id: str,
    worker_name: str,
    *,
    assigned_by: str,
    summary: str,
    file_paths: list[str],
) -> dict | None:
    """Create or replace the active assignment for one worker."""
    swarm = await _fetch_swarm_row(conn, swarm_id)
    if swarm is None:
        return None

    workers = await _fetch_workers_for_swarms(conn, [swarm_id])
    lead_workers = _lead_workers(workers)
    if len(lead_workers) != 1:
        raise ValueError("Assignments require a collaborative swarm with exactly one lead")

    lead = lead_workers[0]
    if assigned_by not in {lead["name"], lead["session"]}:
        raise ValueError("Only the swarm lead can issue assignments")

    worker = await _fetch_worker_row(conn, swarm_id, worker_name)
    if worker is None:
        return None
    if worker["role"] == "lead":
        raise ValueError("Lead workers cannot receive implementation assignments")
    if worker["status"] in _TERMINAL_WORKER_STATUSES:
        raise ValueError("Cannot assign work to a completed worker")

    now = _now_iso()
    active_assignments = await _fetch_active_assignments(conn, swarm_id)
    conflicting_paths = _find_file_conflicts(active_assignments, worker_name, file_paths)
    if conflicting_paths:
        conflicted_path, conflicting_worker = conflicting_paths[0]
        raise ValueError(
            f"File '{conflicted_path}' is already assigned to worker '{conflicting_worker}'"
        )

    await _close_active_assignments_for_worker(
        conn,
        swarm_id,
        worker_name,
        assignment_status="superseded",
        completed_at=now,
    )

    result = await conn.execute(
        insert(SwarmAssignmentORM)
        .values(
            swarm_id=swarm_id,
            worker_name=worker_name,
            assigned_by=assigned_by,
            summary=summary,
            file_paths=_encode_file_paths(file_paths),
            status="active",
            created_at=now,
            completed_at=None,
        )
        .returning(*SwarmAssignmentORM.__table__.c)
    )
    return _decode_assignment_row(dict(result.fetchone()._mapping))


async def list_assignments(conn: AsyncConnection, swarm_id: str) -> list[dict]:
    """List all swarm assignments in timestamp order."""
    result = await conn.execute(
        select(SwarmAssignmentORM.__table__)
        .where(SwarmAssignmentORM.swarm_id == swarm_id)
        .order_by(SwarmAssignmentORM.created_at.asc(), SwarmAssignmentORM.assignment_id.asc())
    )
    return [_decode_assignment_row(dict(row._mapping)) for row in result.fetchall()]


async def record_swarm_message(
    conn: AsyncConnection,
    swarm_id: str,
    *,
    target_kind: str,
    from_entity: str,
    message: str,
    delivered: int,
    failed: int,
    total: int,
    target_role: str | None = None,
    target_worker_name: str | None = None,
) -> dict:
    """Persist one swarm message log entry."""
    now = _now_iso()
    result = await conn.execute(
        insert(SwarmMessageORM)
        .values(
            swarm_id=swarm_id,
            target_kind=target_kind,
            target_role=target_role,
            target_worker_name=target_worker_name,
            from_entity=from_entity,
            message=message,
            delivered=delivered,
            failed=failed,
            total=total,
            created_at=now,
        )
        .returning(*SwarmMessageORM.__table__.c)
    )
    return dict(result.fetchone()._mapping)


async def list_swarm_messages(conn: AsyncConnection, swarm_id: str) -> list[dict]:
    """List all swarm messages in timestamp order."""
    result = await conn.execute(
        select(SwarmMessageORM.__table__)
        .where(SwarmMessageORM.swarm_id == swarm_id)
        .order_by(SwarmMessageORM.created_at.asc(), SwarmMessageORM.message_id.asc())
    )
    return [dict(row._mapping) for row in result.fetchall()]


async def reconcile_swarm_worker_sessions(
    conn: AsyncConnection,
    active_sessions: set[str],
) -> int:
    """Mark session-bearing workers failed when their tmux session disappears."""
    result = await conn.execute(
        select(
            SwarmWorkerORM.swarm_id,
            SwarmWorkerORM.name,
            SwarmWorkerORM.session,
        ).where(SwarmWorkerORM.status.in_(("started", "working", "pr_created")))
    )
    candidate_rows = _rows_to_dicts(result)
    lost_workers = [row for row in candidate_rows if row["session"] not in active_sessions]
    if not lost_workers:
        return 0

    touched_swarms: set[str] = set()
    for worker in lost_workers:
        await complete_worker(
            conn,
            worker["swarm_id"],
            worker["name"],
            "failed",
            "Worker session exited before reporting completion.",
            failure_reason="session_lost",
        )
        touched_swarms.add(worker["swarm_id"])

    for swarm_id in touched_swarms:
        await _auto_promote_validating_if_ready(
            conn,
            swarm_id,
            triggered_by="system:session_lost",
        )
    return len(lost_workers)


async def _fetch_swarm_row(conn: AsyncConnection, swarm_id: str) -> dict | None:
    result = await conn.execute(select(SwarmORM.__table__).where(SwarmORM.swarm_id == swarm_id))
    row = result.fetchone()
    return dict(row._mapping) if row else None


async def _fetch_worker_row(
    conn: AsyncConnection,
    swarm_id: str,
    worker_name: str,
) -> dict | None:
    result = await conn.execute(
        select(SwarmWorkerORM.__table__).where(
            SwarmWorkerORM.swarm_id == swarm_id,
            SwarmWorkerORM.name == worker_name,
        )
    )
    row = result.fetchone()
    return dict(row._mapping) if row else None


async def _fetch_active_assignment_row(
    conn: AsyncConnection,
    swarm_id: str,
    worker_name: str,
) -> dict | None:
    result = await conn.execute(
        select(SwarmAssignmentORM.__table__)
        .where(
            SwarmAssignmentORM.swarm_id == swarm_id,
            SwarmAssignmentORM.worker_name == worker_name,
            SwarmAssignmentORM.status == "active",
        )
        .order_by(SwarmAssignmentORM.created_at.desc(), SwarmAssignmentORM.assignment_id.desc())
        .limit(1)
    )
    row = result.fetchone()
    if row is None:
        return None
    return _decode_assignment_row(dict(row._mapping))


async def _fetch_active_assignments(conn: AsyncConnection, swarm_id: str) -> list[dict]:
    result = await conn.execute(
        select(SwarmAssignmentORM.__table__)
        .where(
            SwarmAssignmentORM.swarm_id == swarm_id,
            SwarmAssignmentORM.status == "active",
        )
        .order_by(SwarmAssignmentORM.created_at.asc(), SwarmAssignmentORM.assignment_id.asc())
    )
    return [_decode_assignment_row(dict(row._mapping)) for row in result.fetchall()]


def _find_file_conflicts(
    assignments: list[dict],
    worker_name: str,
    file_paths: list[str],
) -> list[tuple[str, str]]:
    if not file_paths:
        return []
    conflicts: list[tuple[str, str]] = []
    requested_paths = set(file_paths)
    for assignment in assignments:
        if assignment["worker_name"] == worker_name:
            continue
        for path in assignment.get("file_paths", []):
            if path in requested_paths:
                conflicts.append((path, assignment["worker_name"]))
    return conflicts


async def _fetch_workers_for_swarms(
    conn: AsyncConnection,
    swarm_ids: list[str],
) -> list[dict]:
    if not swarm_ids:
        return []
    result = await conn.execute(
        select(SwarmWorkerORM.__table__)
        .where(SwarmWorkerORM.swarm_id.in_(swarm_ids))
        .order_by(SwarmWorkerORM.created_at.asc(), SwarmWorkerORM.name.asc())
    )
    return _rows_to_dicts(result)


async def _close_active_assignments_for_worker(
    conn: AsyncConnection,
    swarm_id: str,
    worker_name: str,
    *,
    assignment_status: str,
    completed_at: str,
) -> None:
    if assignment_status not in _TERMINAL_ASSIGNMENT_STATUSES:
        raise ValueError(f"Unsupported terminal assignment status: {assignment_status}")
    await conn.execute(
        update(SwarmAssignmentORM)
        .where(
            SwarmAssignmentORM.swarm_id == swarm_id,
            SwarmAssignmentORM.worker_name == worker_name,
            SwarmAssignmentORM.status == "active",
        )
        .values(status=assignment_status, completed_at=completed_at)
    )


async def _fetch_phase_history_for_swarms(
    conn: AsyncConnection,
    swarm_ids: list[str],
) -> list[dict]:
    if not swarm_ids:
        return []
    result = await conn.execute(
        select(SwarmPhaseHistoryORM.__table__)
        .where(SwarmPhaseHistoryORM.swarm_id.in_(swarm_ids))
        .order_by(SwarmPhaseHistoryORM.timestamp.asc(), SwarmPhaseHistoryORM.history_id.asc())
    )
    return _rows_to_dicts(result)


async def _insert_phase_history(
    conn: AsyncConnection,
    *,
    swarm_id: str,
    from_phase: str | None,
    to_phase: str,
    triggered_by: str,
    timestamp: str,
) -> None:
    await conn.execute(
        insert(SwarmPhaseHistoryORM).values(
            swarm_id=swarm_id,
            from_phase=from_phase,
            to_phase=to_phase,
            timestamp=timestamp,
            triggered_by=triggered_by,
        )
    )


async def _set_swarm_phase(
    conn: AsyncConnection,
    swarm: dict,
    to_phase: str,
    *,
    triggered_by: str,
    validate_transition: bool,
) -> None:
    current_phase = swarm["phase"]
    if current_phase == to_phase:
        return
    if validate_transition and to_phase not in _MANUAL_PHASE_TRANSITIONS.get(current_phase, set()):
        raise ValueError(f"Illegal phase transition: {current_phase} -> {to_phase}")

    now = _now_iso()
    completed_at = _phase_completed_at(swarm.get("completed_at"), to_phase, now)
    await conn.execute(
        update(SwarmORM)
        .where(SwarmORM.swarm_id == swarm["swarm_id"])
        .values(phase=to_phase, completed_at=completed_at)
    )
    await _insert_phase_history(
        conn,
        swarm_id=swarm["swarm_id"],
        from_phase=current_phase,
        to_phase=to_phase,
        triggered_by=triggered_by,
        timestamp=now,
    )


async def _auto_promote_validating_if_ready(
    conn: AsyncConnection,
    swarm_id: str,
    *,
    triggered_by: str,
) -> None:
    swarm = await _fetch_swarm_row(conn, swarm_id)
    if swarm is None:
        return
    workers = await _fetch_workers_for_swarms(conn, [swarm_id])
    if not _can_auto_promote_validating(swarm["phase"], workers):
        return
    await _set_swarm_phase(
        conn,
        swarm,
        "validating",
        triggered_by=triggered_by,
        validate_transition=False,
    )
