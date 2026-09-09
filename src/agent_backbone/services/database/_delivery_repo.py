"""Delivery tracking — every attempt (issue, comment, pull request, direct
message, watch, escalation) with its kind, repository and outcome."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from agent_backbone.models import RETIREMENT_REASONS, RETRYABLE_OUTCOMES
from agent_backbone.services.database._diagnostics_repo import DiagnosticRepo
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso


class DeliveryRepo(Repo):
    async def record(
        self,
        *,
        issue_number: int | None,
        target_entity: str,
        session_name: str,
        outcome: str,
        source: str = "",
        repo: str = "",
        kind: str = "issue",
        preview: str = "",
        operation_id: str | None = None,
    ) -> int:
        """Record a delivery attempt. Returns the row ID."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO deliveries
                       (operation_id, kind, repo, issue_number, target_entity, session_name,
                        outcome, source, preview, created_at)
                       VALUES (:operation_id, :kind, :repo, :issue_number, :target_entity,
                               :session_name, :outcome, :source, :preview, :created_at)
                       RETURNING id"""
                ),
                {
                    "operation_id": operation_id or uuid.uuid4().hex,
                    "kind": kind,
                    "repo": repo,
                    "issue_number": issue_number,
                    "target_entity": target_entity,
                    "session_name": session_name,
                    "outcome": outcome,
                    "source": source,
                    "preview": preview[:200],
                    "created_at": now_iso(),
                },
            )
            return result.scalar_one()

    async def claim(
        self,
        *,
        issue_number: int,
        target_entity: str,
        session_name: str,
        source: str,
        repo: str = "",
        preview: str = "",
        operation_id: str | None = None,
    ) -> int | None:
        """Reserve an issue delivery slot before sending to avoid duplicate sends."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO deliveries
                       (operation_id, kind, repo, issue_number, target_entity, session_name,
                        outcome, source, preview, created_at)
                       VALUES (:operation_id, 'issue', :repo, :issue_number, :target_entity,
                               :session_name, 'attempting', :source, :preview, :now)
                       ON CONFLICT (repo, issue_number, session_name)
                       WHERE kind = 'issue'
                         AND issue_number IS NOT NULL
                         AND outcome IN ('attempting','delivered','retried')
                       DO NOTHING
                       RETURNING id"""
                ),
                {
                    "operation_id": operation_id or uuid.uuid4().hex,
                    "repo": repo,
                    "issue_number": issue_number,
                    "target_entity": target_entity,
                    "session_name": session_name,
                    "source": source,
                    "preview": preview[:200],
                    "now": now_iso(),
                },
            )
            row = result.fetchone()
            return row._mapping["id"] if row else None

    async def finalize(
        self,
        delivery_id: int,
        outcome: str,
        *,
        operation_id: str | None = None,
    ) -> None:
        """Finalize a claimed delivery attempt."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """UPDATE deliveries SET outcome = :outcome,
                       operation_id = COALESCE(:operation_id, operation_id)
                       WHERE id = :id AND outcome = 'attempting'"""
                ),
                {"id": delivery_id, "outcome": outcome, "operation_id": operation_id},
            )

    async def reclaim_stale(
        self,
        max_age_minutes: int = 5,
    ) -> int:
        """Delete stale attempting rows so new delivery claims can proceed."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """DELETE FROM deliveries
                       WHERE outcome = 'attempting' AND created_at < :cutoff"""
                ),
                {"cutoff": cutoff_iso(minutes=max_age_minutes)},
            )
            return result.rowcount

    async def query(
        self,
        issue_number: int | None = None,
        target_entity: str | None = None,
        session_name: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        *,
        repo: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        """Query delivery records with optional filters."""
        async with self._tx() as conn:
            conditions: list[str] = []
            params: dict[str, object] = {}
            if issue_number is not None:
                conditions.append("issue_number = :issue_number")
                params["issue_number"] = issue_number
            if repo is not None:
                conditions.append("repo = :repo")
                params["repo"] = repo
            if kind is not None:
                conditions.append("kind = :kind")
                params["kind"] = kind
            if target_entity is not None:
                conditions.append("target_entity = :target_entity")
                params["target_entity"] = target_entity
            if session_name is not None:
                conditions.append("session_name = :session_name")
                params["session_name"] = session_name
            if outcome is not None:
                conditions.append("outcome = :outcome")
                params["outcome"] = outcome

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            sql = f"SELECT * FROM deliveries {where} ORDER BY created_at DESC, id DESC LIMIT :lim"
            params["lim"] = limit

            result = await conn.execute(text(sql), params)
            return [dict(row._mapping) for row in result.fetchall()]

    async def failed(
        self,
        limit: int = 50,
    ) -> list[dict]:
        """Issue deliveries whose latest outcome is retryable (no later success)."""
        async with self._tx() as conn:
            placeholders = ",".join(f"'{o.value}'" for o in sorted(RETRYABLE_OUTCOMES))
            result = await conn.execute(
                text(
                    f"""SELECT d.* FROM deliveries d
                       WHERE d.kind = 'issue' AND d.source != 'github-outbox'
                         AND d.issue_number IS NOT NULL
                         AND d.outcome IN ({placeholders})
                         AND NOT EXISTS (
                           SELECT 1 FROM deliveries d2
                           WHERE d2.kind = 'issue'
                             AND d2.repo = d.repo
                             AND d2.issue_number = d.issue_number
                             AND d2.target_entity = d.target_entity
                             AND d2.id > d.id
                         )
                       ORDER BY d.created_at ASC LIMIT :lim"""
                ),
                {"lim": limit},
            )
            return [dict(row._mapping) for row in result.fetchall()]

    async def retire(self, delivery_id: int, outcome: str) -> None:
        """Keep a failure's history but remove terminal work from the retry window.

        Only retryable attempts can change; an overlapping successful delivery
        or another retirement must never be overwritten.
        """
        if outcome not in RETIREMENT_REASONS:
            raise ValueError(f"Not a terminal retry outcome: {outcome}")
        placeholders = ",".join(f"'{o.value}'" for o in sorted(RETRYABLE_OUTCOMES))
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "UPDATE deliveries SET outcome = :outcome, "
                    "operation_id = COALESCE(operation_id, :operation_id) "
                    f"WHERE id = :id AND kind = 'issue' AND outcome IN ({placeholders}) RETURNING *"
                ),
                {
                    "id": delivery_id,
                    "outcome": outcome,
                    "operation_id": uuid.uuid5(
                        uuid.NAMESPACE_URL, f"backbone:delivery:{delivery_id}"
                    ).hex,
                },
            )
            row = result.mappings().first()
        if row is not None:
            await DiagnosticRepo(self._engine).record(
                category="delivery",
                code=f"retired_{outcome}",
                operation_id=row["operation_id"],
                severity="info",
                agent_name=row["session_name"],
                source=row["source"],
                repo=row["repo"],
                issue_number=row["issue_number"],
                delivery_id=delivery_id,
                details={"delivery_kind": row["kind"], "reason": outcome},
            )

    async def prune(
        self,
        retention_days: int = 30,
    ) -> int:
        """Delete delivery records older than retention period. Returns count deleted."""
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM deliveries WHERE created_at < :cutoff"),
                {"cutoff": cutoff_iso(days=retention_days)},
            )
            return result.rowcount

    async def stats(self) -> list[dict]:
        """Get delivery counts grouped by outcome."""
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT outcome, COUNT(*) as cnt FROM deliveries GROUP BY outcome")
            )
            return [dict(row._mapping) for row in result.fetchall()]
