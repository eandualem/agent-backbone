"""Agent transitions — explicit one-time stop/restart requests the backbone owns."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso

OPEN = "pending"


def _row(row) -> dict:
    data = dict(row._mapping)
    data["resume"] = bool(data["resume"])
    data["start"] = bool(data["start"])
    try:
        data["result"] = json.loads(data.get("result") or "{}")
    except ValueError:
        data["result"] = {}
    return data


class TransitionRepo(Repo):
    async def create(
        self,
        *,
        agent_name: str,
        requested_by: str = "",
        runtime: str | None = None,
        model: str | None = None,
        resume: bool = False,
        start: bool = True,
        delay_seconds: int = 60,
        start_at: str | None = None,
        message: str | None = None,
    ) -> dict | None:
        """Persist an accepted request as ``pending`` and return the row, or None
        when the agent already has a pending one (``uq_transitions_open``)."""
        try:
            async with self._tx() as conn:
                result = await conn.execute(
                    text(
                        """INSERT INTO agent_transitions
                       (agent_name, requested_by, requested_at, runtime, model, resume, start,
                        delay_seconds, start_at, message, status, result)
                       VALUES (:agent_name, :requested_by, :requested_at, :runtime, :model,
                               :resume, :start, :delay_seconds, :start_at, :message,
                               'pending', '{}')
                       RETURNING *"""
                    ),
                    {
                        "agent_name": agent_name,
                        "requested_by": requested_by,
                        "requested_at": now_iso(),
                        "runtime": runtime,
                        "model": model,
                        "resume": int(resume),
                        "start": int(start),
                        "delay_seconds": delay_seconds,
                        "start_at": start_at,
                        "message": message,
                    },
                )
                return _row(result.fetchone())
        except IntegrityError:
            return None

    async def get(self, transition_id: int) -> dict | None:
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT * FROM agent_transitions WHERE id = :id"), {"id": transition_id}
            )
            row = result.fetchone()
            return _row(row) if row else None

    async def open_for(self, agent_name: str) -> dict | None:
        """The agent's pending transition, if any (at most one is accepted)."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT * FROM agent_transitions WHERE agent_name = :agent_name"
                    " AND status = 'pending' ORDER BY id LIMIT 1"
                ),
                {"agent_name": agent_name},
            )
            row = result.fetchone()
            return _row(row) if row else None

    async def open_agents(self) -> list[str]:
        """Names of agents with a pending transition (a planned absence)."""
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT DISTINCT agent_name FROM agent_transitions WHERE status = 'pending'")
            )
            return [row[0] for row in result.fetchall()]

    async def pending(self) -> list[dict]:
        """Every pending transition, oldest first."""
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT * FROM agent_transitions WHERE status = 'pending' ORDER BY id")
            )
            return [_row(row) for row in result.fetchall()]

    async def list_for(self, agent_name: str, limit: int = 5) -> list[dict]:
        """Recent transitions for one agent, newest first."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT * FROM agent_transitions WHERE agent_name = :agent_name"
                    " ORDER BY id DESC LIMIT :limit"
                ),
                {"agent_name": agent_name, "limit": limit},
            )
            return [_row(row) for row in result.fetchall()]

    async def mark_stopped(self, transition_id: int, *, start_at: str | None) -> None:
        """The session is gone; ``start_at`` is when the replacement is due."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """UPDATE agent_transitions
                       SET stopped_at = :now, start_at = COALESCE(start_at, :start_at)
                       WHERE id = :id AND status = 'pending'"""
                ),
                {"id": transition_id, "now": now_iso(), "start_at": start_at},
            )

    async def mark_launching(self, transition_id: int, operation_id: str) -> None:
        """Remember the startup operation about to run for this transition."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """UPDATE agent_transitions SET launch_operation_id = :operation_id
                       WHERE id = :id AND status = 'pending'"""
                ),
                {"id": transition_id, "operation_id": operation_id},
            )

    async def finish(self, transition_id: int, status: str, result: dict) -> None:
        """Close the transition as ``completed`` or ``failed`` with its result."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """UPDATE agent_transitions
                       SET status = :status, completed_at = :now, result = :result
                       WHERE id = :id AND status = 'pending'"""
                ),
                {
                    "id": transition_id,
                    "status": status,
                    "now": now_iso(),
                    "result": json.dumps(result, sort_keys=True),
                },
            )
