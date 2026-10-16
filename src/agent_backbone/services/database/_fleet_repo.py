"""Fleet snapshots — the running agents and their conversations, saved to resume later."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


def _row(row) -> dict:
    data = dict(row._mapping)
    data["stop"] = bool(data["stop"])
    data["force"] = bool(data["force"])
    for key in ("agents", "resumes"):
        try:
            data[key] = json.loads(data.get(key) or "[]")
        except ValueError:
            data[key] = []
    return data


class FleetSnapshotRepo(Repo):
    def __init__(self, engine) -> None:
        super().__init__(engine)
        # The backbone is one process: this serializes the history's
        # read-modify-write, which a transaction alone does not.
        self._resume_lock = asyncio.Lock()

    async def create(
        self,
        *,
        created_by: str,
        note: str | None,
        stop: bool,
        force: bool,
        agents: list[dict],
    ) -> dict:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO fleet_snapshots
                       (created_at, created_by, note, stop, force, agents, resumes)
                       VALUES (:created_at, :created_by, :note, :stop, :force, :agents, '[]')
                       RETURNING *"""
                ),
                {
                    "created_at": now_iso(),
                    "created_by": created_by,
                    "note": note,
                    "stop": int(stop),
                    "force": int(force),
                    "agents": json.dumps(agents),
                },
            )
            return _row(result.fetchone())

    async def set_agents(self, snapshot_id: int, agents: list[dict]) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE fleet_snapshots SET agents = :agents WHERE id = :id"),
                {"id": snapshot_id, "agents": json.dumps(agents)},
            )

    async def add_resume(self, snapshot_id: int, run: dict) -> None:
        """Append a resume run; two runs finishing together both stay."""
        async with self._resume_lock, self._tx() as conn:
            result = await conn.execute(
                text("SELECT resumes FROM fleet_snapshots WHERE id = :id"), {"id": snapshot_id}
            )
            row = result.fetchone()
            if row is None:
                return
            try:
                runs = json.loads(row[0] or "[]")
            except ValueError:
                runs = []
            runs.append(run)
            await conn.execute(
                text("UPDATE fleet_snapshots SET resumes = :resumes WHERE id = :id"),
                {"id": snapshot_id, "resumes": json.dumps(runs)},
            )

    async def get(self, snapshot_id: int) -> dict | None:
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT * FROM fleet_snapshots WHERE id = :id"), {"id": snapshot_id}
            )
            row = result.fetchone()
            return _row(row) if row else None

    async def latest(self) -> dict | None:
        rows = await self.recent(limit=1)
        return rows[0] if rows else None

    async def recent(self, limit: int = 20) -> list[dict]:
        """Snapshots, newest first."""
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT * FROM fleet_snapshots ORDER BY id DESC LIMIT :limit"),
                {"limit": limit},
            )
            return [_row(row) for row in result.fetchall()]
