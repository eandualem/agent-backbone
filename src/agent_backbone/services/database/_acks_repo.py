"""Acknowledgements — who has commented on which issue (per repository)."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


class AcknowledgementRepo(Repo):
    async def record(self, issue_number: int, target_entity: str, *, repo: str = "") -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO acknowledgments
                       (repo, issue_number, target_entity, acknowledged_at)
                       VALUES (:repo, :issue_number, :target_entity, :acknowledged_at)
                       ON CONFLICT(repo, issue_number, target_entity) DO UPDATE SET
                         acknowledged_at = excluded.acknowledged_at"""
                ),
                {
                    "repo": repo,
                    "issue_number": issue_number,
                    "target_entity": target_entity,
                    "acknowledged_at": now_iso(),
                },
            )

    async def exists(self, issue_number: int, target_entity: str, *, repo: str = "") -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM acknowledgments WHERE repo = :repo"
                    " AND issue_number = :issue_number AND target_entity = :target_entity"
                ),
                {"repo": repo, "issue_number": issue_number, "target_entity": target_entity},
            )
            return result.fetchone() is not None

    async def clear(self, issue_number: int, target_entity: str, *, repo: str = "") -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "DELETE FROM acknowledgments WHERE repo = :repo"
                    " AND issue_number = :issue_number AND target_entity = :target_entity"
                ),
                {"repo": repo, "issue_number": issue_number, "target_entity": target_entity},
            )
