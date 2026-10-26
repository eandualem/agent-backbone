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
        comment_id: int | None = None,
        registration_id: str | None = None,
    ) -> bool:
        async with self._tx() as conn:
            if registration_id is not None and conn.dialect.name == "postgresql":
                # Lock registration before the acknowledgment, as forget/rename do.
                # An EXISTS snapshot alone can outlive a concurrent retirement.
                current = await conn.execute(
                    text(
                        "SELECT 1 FROM agents WHERE name=:name "
                        "AND report_identity=:identity FOR UPDATE"
                    ),
                    {"name": target_entity, "identity": registration_id},
                )
                if not current.first():
                    return False
            result = await conn.execute(
                text(
                    """INSERT INTO acknowledgments
                       (repo, issue_number, target_entity, acknowledged_at)
                       SELECT :repo, :issue_number, :target_entity, :acknowledged_at
                       WHERE CAST(:registration_id AS TEXT) IS NULL OR EXISTS (
                         SELECT 1 FROM agents WHERE name=:target_entity
                           AND report_identity=:registration_id)
                       ON CONFLICT(repo, issue_number, target_entity) DO UPDATE SET
                         acknowledged_at = excluded.acknowledged_at
                       WHERE (CAST(:not_cleared_since AS TEXT) IS NULL
                          OR acknowledgments.cleared_at IS NULL
                          OR acknowledgments.cleared_at <= :not_cleared_since)
                         AND (CAST(:comment_id AS BIGINT) IS NULL
                           OR acknowledgments.cleared_comment_id IS NULL
                           OR :comment_id >= acknowledgments.cleared_comment_id)"""
                ),
                {
                    "repo": repo,
                    "issue_number": issue_number,
                    "target_entity": target_entity,
                    "acknowledged_at": now_iso(),
                    "not_cleared_since": not_cleared_since,
                    "comment_id": comment_id,
                    "registration_id": registration_id,
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

    async def reset_boundary(
        self, issue_number: int, target_entity: str, *, repo: str = ""
    ) -> tuple[str, int | None]:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT cleared_at, cleared_comment_id FROM acknowledgments WHERE repo=:repo "
                    "AND issue_number=:issue AND target_entity=:target"
                ),
                {"repo": repo, "issue": issue_number, "target": target_entity},
            )
            row = result.fetchone()
            return (row[0] or "", row[1]) if row else ("", None)

    async def clear(
        self,
        issue_number: int,
        target_entity: str,
        *,
        repo: str = "",
        comment_id: int | None = None,
    ) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO acknowledgments "
                    "(repo,issue_number,target_entity,acknowledged_at,"
                    "cleared_at,cleared_comment_id) "
                    "VALUES (:repo,:issue_number,:target_entity,'',:now,:comment_id) "
                    "ON CONFLICT(repo,issue_number,target_entity) DO UPDATE SET "
                    "acknowledged_at='', cleared_at=excluded.cleared_at, "
                    "cleared_comment_id=excluded.cleared_comment_id "
                    "WHERE acknowledgments.cleared_comment_id IS NULL "
                    "OR excluded.cleared_comment_id IS NULL "
                    "OR excluded.cleared_comment_id > acknowledgments.cleared_comment_id"
                ),
                {
                    "repo": repo,
                    "issue_number": issue_number,
                    "target_entity": target_entity,
                    "now": now_iso(),
                    "comment_id": comment_id,
                },
            )
