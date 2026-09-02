"""Swarms — lifecycle records for coordinator+members worktree swarms."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


class SwarmRepo(Repo):
    async def create(
        self,
        name: str,
        *,
        repo: str,
        issue_number: int,
        initiator: str,
        coordinator: str,
        branch: str,
        worktree_dir: str,
    ) -> None:
        # A finished swarm's name may be reused: replace the completed record.
        # The interface refuses reuse while the old swarm is still active, and
        # the uq_swarms_active_issue index guards the issue either way.
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO swarms
                       (name, repo, issue_number, initiator, coordinator, branch,
                        worktree_dir, status, created_at)
                       VALUES (:name, :repo, :issue, :initiator, :coordinator, :branch,
                               :worktree, 'active', :now)
                       ON CONFLICT (name) DO UPDATE SET
                         repo = excluded.repo,
                         issue_number = excluded.issue_number,
                         initiator = excluded.initiator,
                         coordinator = excluded.coordinator,
                         branch = excluded.branch,
                         worktree_dir = excluded.worktree_dir,
                         status = 'active',
                         created_at = excluded.created_at,
                         completed_at = NULL
                       WHERE swarms.status != 'active'"""
                ),
                {
                    "name": name,
                    "repo": repo,
                    "issue": issue_number,
                    "initiator": initiator,
                    "coordinator": coordinator,
                    "branch": branch,
                    "worktree": worktree_dir,
                    "now": now_iso(),
                },
            )

    async def get(self, name: str) -> dict | None:
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT * FROM swarms WHERE name = :name"), {"name": name}
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def list(self, *, active_only: bool = False) -> list[dict]:
        async with self._tx() as conn:
            sql = "SELECT * FROM swarms"
            if active_only:
                sql += " WHERE status = 'active'"
            result = await conn.execute(text(sql + " ORDER BY created_at DESC"))
            return [dict(row._mapping) for row in result.fetchall()]

    async def active_for_issue(self, repo: str, issue_number: int) -> dict | None:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """SELECT * FROM swarms
                       WHERE repo = :repo AND issue_number = :issue AND status = 'active'"""
                ),
                {"repo": repo, "issue": issue_number},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def set_status(self, name: str, status: str) -> None:
        async with self._tx() as conn:
            completed = now_iso() if status in ("done", "disbanded") else None
            await conn.execute(
                text(
                    """UPDATE swarms SET status = :status,
                       completed_at = COALESCE(:completed, completed_at)
                       WHERE name = :name"""
                ),
                {"name": name, "status": status, "completed": completed},
            )
