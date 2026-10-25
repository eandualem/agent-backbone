"""Acknowledgements — who has commented on which issue (per repository)."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


class AcknowledgementRepo(Repo):
    async def record(
        self,
        issue_number: int,
        target_entity: str,
        *,
        repo: str = "",
        not_cleared_since: str | None = None,
    ) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO acknowledgments
                       (repo, issue_number, target_entity, acknowledged_at)
                       VALUES (:repo, :issue_number, :target_entity, :acknowledged_at)
                       ON CONFLICT(repo, issue_number, target_entity) DO UPDATE SET
                         acknowledged_at = excluded.acknowledged_at
                       WHERE :not_cleared_since IS NULL OR acknowledgments.cleared_at IS NULL
                          OR acknowledgments.cleared_at <= :not_cleared_since"""
                ),
                {
                    "repo": repo,
                    "issue_number": issue_number,
                    "target_entity": target_entity,
                    "acknowledged_at": now_iso(),
                    "not_cleared_since": not_cleared_since,
                },
            )
            return bool(result.rowcount)

    async def exists(self, issue_number: int, target_entity: str, *, repo: str = "") -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM acknowledgments WHERE repo = :repo"
                    " AND issue_number = :issue_number AND target_entity = :target_entity"
                    " AND acknowledged_at != ''"
                ),
                {"repo": repo, "issue_number": issue_number, "target_entity": target_entity},
            )
            return result.fetchone() is not None

    async def cleared_at(self, issue_number: int, target_entity: str, *, repo: str = "") -> str:
        async with self._tx() as conn:
            return (
                await conn.scalar(
                    text(
                        "SELECT cleared_at FROM acknowledgments WHERE repo=:repo "
                        "AND issue_number=:issue AND target_entity=:target"
                    ),
                    {"repo": repo, "issue": issue_number, "target": target_entity},
                )
            ) or ""

    async def clear(self, issue_number: int, target_entity: str, *, repo: str = "") -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO acknowledgments "
                    "(repo,issue_number,target_entity,acknowledged_at,cleared_at) "
                    "VALUES (:repo,:issue_number,:target_entity,'',:now) "
                    "ON CONFLICT(repo,issue_number,target_entity) DO UPDATE SET "
                    "acknowledged_at='', cleared_at=excluded.cleared_at"
                ),
                {
                    "repo": repo,
                    "issue_number": issue_number,
                    "target_entity": target_entity,
                    "now": now_iso(),
                },
            )
