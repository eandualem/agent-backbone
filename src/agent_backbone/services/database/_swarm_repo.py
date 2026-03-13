"""Swarm registry repository."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_backbone.services.database._repo_utils import (
    named_placeholders,
    row_to_dict,
    rows_to_dicts,
    utc_now_iso,
)

_ACTIVE_SWARM_STATUSES = {"active", "completing", "failed"}


def _group_workers(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["swarm_id"], []).append(row)
    return grouped


def _compute_swarm_status(worker_statuses: list[str]) -> str:
    if not worker_statuses:
        return "active"
    if all(status == "done" for status in worker_statuses):
        return "completing"
    if all(status in {"done", "failed"} for status in worker_statuses) and any(
        status == "failed" for status in worker_statuses
    ):
        return "failed"
    return "active"


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


def _build_swarm_detail(swarm: dict, workers: list[dict]) -> dict:
    ordered_workers = sorted(workers, key=lambda worker: (worker["created_at"], worker["name"]))
    return {
        **swarm,
        "worker_count": len(ordered_workers),
        "progress": _build_progress(ordered_workers),
        "workers": ordered_workers,
    }


async def create_swarm(
    conn: AsyncConnection,
    repo: str,
    task_id: str | None,
    coding_agent_session: str,
    workers: list[dict],
) -> str:
    """Create a swarm and its workers. Returns the swarm ID."""
    now = utc_now_iso()
    swarm_id = str(uuid.uuid4())
    await conn.execute(
        text(
            """INSERT INTO swarms
               (swarm_id, repo, task_id, coding_agent_session, status, created_at, completed_at)
               VALUES (
                   :swarm_id,
                   :repo,
                   :task_id,
                   :coding_agent_session,
                   :status,
                   :created_at,
                   NULL
               )"""
        ),
        {
            "swarm_id": swarm_id,
            "repo": repo,
            "task_id": task_id,
            "coding_agent_session": coding_agent_session,
            "status": "active",
            "created_at": now,
        },
    )

    for worker in workers:
        await conn.execute(
            text(
                """INSERT INTO swarm_workers
                   (worker_id, swarm_id, name, branch, worktree_path, session,
                    status, pr_number, created_at, updated_at)
                   VALUES (:worker_id, :swarm_id, :name, :branch, :worktree_path, :session,
                           :status, :pr_number, :created_at, :updated_at)"""
            ),
            {
                "worker_id": str(uuid.uuid4()),
                "swarm_id": swarm_id,
                "name": worker["name"],
                "branch": worker["branch"],
                "worktree_path": worker["worktree_path"],
                "session": worker["session"],
                "status": "pending",
                "pr_number": None,
                "created_at": now,
                "updated_at": now,
            },
        )
    return swarm_id


async def list_swarms(
    conn: AsyncConnection,
    repo: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List swarms with aggregated worker progress."""
    conditions: list[str] = []
    params: dict[str, object] = {}
    if repo:
        conditions.append("repo = :repo")
        params["repo"] = repo
    if status:
        conditions.append("status = :status")
        params["status"] = status
    else:
        placeholders, active_status_params = named_placeholders(
            "status",
            sorted(_ACTIVE_SWARM_STATUSES),
        )
        params.update(active_status_params)
        conditions.append(f"status IN ({placeholders})")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    result = await conn.execute(
        text(f"SELECT * FROM swarms {where} ORDER BY created_at DESC"),
        params,
    )
    swarms = rows_to_dicts(result.fetchall())
    if not swarms:
        return []

    swarm_ids = [swarm["swarm_id"] for swarm in swarms]
    worker_rows = await _fetch_workers_for_swarms(conn, swarm_ids)
    grouped_workers = _group_workers(worker_rows)
    return [
        _build_swarm_detail(swarm, grouped_workers.get(swarm["swarm_id"], []))
        for swarm in swarms
    ]


async def get_swarm(
    conn: AsyncConnection,
    swarm_id: str,
) -> dict | None:
    """Get a single swarm with worker details."""
    result = await conn.execute(
        text("SELECT * FROM swarms WHERE swarm_id = :swarm_id"),
        {"swarm_id": swarm_id},
    )
    swarm = row_to_dict(result.fetchone())
    if swarm is None:
        return None

    workers = await _fetch_workers_for_swarms(conn, [swarm_id])
    return _build_swarm_detail(swarm, workers)


async def update_worker_status(
    conn: AsyncConnection,
    swarm_id: str,
    worker_name: str,
    status: str,
    pr_number: int | None = None,
) -> dict | None:
    """Update a worker status and recompute swarm aggregate status."""
    now = utc_now_iso()
    result = await conn.execute(
        text(
            """UPDATE swarm_workers
               SET status = :status,
                   pr_number = COALESCE(:pr_number, pr_number),
                   updated_at = :updated_at
               WHERE swarm_id = :swarm_id AND name = :worker_name
               RETURNING *"""
        ),
        {
            "swarm_id": swarm_id,
            "worker_name": worker_name,
            "status": status,
            "pr_number": pr_number,
            "updated_at": now,
        },
    )
    updated = result.fetchone()
    if updated is None:
        return None

    workers = await _fetch_workers_for_swarms(conn, [swarm_id])
    swarm_status = _compute_swarm_status([worker["status"] for worker in workers])
    await conn.execute(
        text(
            """UPDATE swarms
               SET status = :status
               WHERE swarm_id = :swarm_id AND status != 'completed'"""
        ),
        {"swarm_id": swarm_id, "status": swarm_status},
    )

    return await get_swarm(conn, swarm_id)


async def complete_swarm(
    conn: AsyncConnection,
    swarm_id: str,
) -> dict | None:
    """Mark a swarm completed."""
    result = await conn.execute(
        text(
            """UPDATE swarms
               SET status = 'completed',
                   completed_at = :completed_at
               WHERE swarm_id = :swarm_id
               RETURNING swarm_id"""
        ),
        {"swarm_id": swarm_id, "completed_at": utc_now_iso()},
    )
    if result.fetchone() is None:
        return None
    return await get_swarm(conn, swarm_id)


async def _fetch_workers_for_swarms(
    conn: AsyncConnection,
    swarm_ids: list[str],
) -> list[dict]:
    if not swarm_ids:
        return []
    placeholders, params = named_placeholders("swarm_id", swarm_ids)
    result = await conn.execute(
        text(
            "SELECT * FROM swarm_workers "
            f"WHERE swarm_id IN ({placeholders}) "
            "ORDER BY created_at ASC, name ASC"
        ),
        params,
    )
    return rows_to_dicts(result.fetchall())
