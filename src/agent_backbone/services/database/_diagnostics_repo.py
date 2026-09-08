"""Bounded operational evidence; payloads and free-text evidence never enter this channel."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso
from agent_backbone.services.database.models import DeliveryORM, DiagnosticORM, MessageQueueORM

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"[A-Za-z0-9_./:@+\-]*\Z")
_TOKEN_DETAILS = frozenset(
    {
        "stage",
        "requested_runtime",
        "requested_model",
        "resume_selection",
        "state",
        "state_source",
        "reason",
        "error_type",
        "model_source",
        "condition",
        "queue_status",
        "delivery_kind",
        "job",
        "observed_model",
        "observed_effort",
    }
)
_BOOL_DETAILS = frozenset({"resume", "priority", "requeue"})
_INT_DETAILS = frozenset({"duration_ms", "failures", "processed", "http_status"})


def diagnostic_details(value: object) -> dict:
    """Keep only declared scalar codes/counters, never arbitrary evidence or exception text."""
    if not isinstance(value, dict):
        return {}
    clean: dict = {}
    for key, item in value.items():
        if key in _TOKEN_DETAILS and isinstance(item, str) and len(item) <= 160:
            if _TOKEN.fullmatch(item):
                clean[key] = item
        elif (key in _BOOL_DETAILS and isinstance(item, bool)) or (
            key in _INT_DETAILS and type(item) is int and 0 <= item <= 2**63 - 1
        ):
            clean[key] = item
    return clean


def _token(value: str, limit: int = 200) -> str:
    if not isinstance(value, str) or len(value) > limit or not _TOKEN.fullmatch(value):
        raise ValueError("Invalid diagnostic identifier")
    return value


def _optional_token(value: object) -> str:
    """Malformed request metadata must not suppress the failure observation itself."""
    try:
        return _token(value)
    except ValueError:
        return ""


def _row(row) -> dict:
    result = dict(row)
    try:
        result["details"] = diagnostic_details(json.loads(result["details"]))
    except (TypeError, ValueError):
        result["details"] = {}
    return result


class DiagnosticRepo(Repo):
    # Logging the database exception can expose its SQL bound values. A fixed,
    # rate-limited warning is sufficient, including when the database is down.
    _last_warning = float("-inf")
    write_failures = 0

    async def record(
        self,
        *,
        category: str,
        code: str,
        operation_id: str,
        observation_key: str = "",
        severity: str = "info",
        agent_name: str = "",
        source: str = "",
        runtime: str = "",
        model: str | None = None,
        repo: str = "",
        issue_number: int | None = None,
        delivery_id: int | None = None,
        queue_id: int | None = None,
        event_id: int | None = None,
        details: dict | None = None,
    ) -> int | None:
        """Best-effort observation; a recorder failure never changes the operation's outcome.

        Counts cover the retained lifetime of this operation/code, not a sliding
        time interval. Optional correlation references are retained when omitted
        by a later observer (for example a queue drain after outbox intake).
        """
        try:
            if severity not in {"info", "warning", "error"}:
                raise ValueError("Invalid diagnostic severity")
            timestamp = now_iso()
            values = {
                "operation_id": _token(operation_id),
                "observation_key": _token(observation_key),
                "category": _token(category),
                "code": _token(code),
                "severity": severity,
                "agent_name": _optional_token(agent_name),
                "source": _optional_token(source),
                "runtime": _optional_token(runtime),
                "model": _optional_token(model) or None,
                "repo": _optional_token(repo),
                "issue_number": issue_number,
                "delivery_id": delivery_id,
                "queue_id": queue_id,
                "event_id": event_id,
                "first_seen_at": timestamp,
                "last_seen_at": timestamp,
                "occurrences": 1,
                "details": json.dumps(diagnostic_details(details)),
            }
            for key in ("issue_number", "delivery_id", "queue_id", "event_id"):
                if values[key] is not None and (type(values[key]) is not int or values[key] <= 0):
                    raise ValueError("Invalid diagnostic reference")
            if not operation_id or not category or not code:
                raise ValueError("Missing diagnostic identity")
            insert = sqlite_insert if self._engine().dialect.name == "sqlite" else pg_insert
            stmt = insert(DiagnosticORM).values(**values)
            updates = {
                key: value
                for key, value in values.items()
                if key
                not in {
                    "operation_id",
                    "observation_key",
                    "category",
                    "code",
                    "first_seen_at",
                    "occurrences",
                }
            }
            updates["occurrences"] = DiagnosticORM.occurrences + 1
            for key in ("issue_number", "delivery_id", "queue_id", "event_id", "model"):
                updates[key] = func.coalesce(
                    getattr(stmt.excluded, key), getattr(DiagnosticORM, key)
                )
            for key in ("agent_name", "source", "runtime", "repo"):
                if not values[key]:
                    updates.pop(key)
            stmt = stmt.on_conflict_do_update(
                index_elements=["operation_id", "category", "code", "observation_key"],
                set_=updates,
            )
            async with self._tx() as conn:
                await conn.execute(stmt)
                result = await conn.execute(
                    select(DiagnosticORM.id).where(
                        DiagnosticORM.operation_id == operation_id,
                        DiagnosticORM.observation_key == observation_key,
                        DiagnosticORM.category == category,
                        DiagnosticORM.code == code,
                    )
                )
                return result.scalar_one()
        except Exception:
            type(self).write_failures += 1
            stamp = time.monotonic()
            if stamp - type(self)._last_warning >= 60:
                type(self)._last_warning = stamp
                log.warning("Operational diagnostic could not be recorded; evidence is incomplete")
            return None

    @staticmethod
    def _where(*, since=None, agent_name=None, category=None, severity=None, operation_id=None):
        filters = []
        for name, value in (
            ("agent_name", agent_name),
            ("category", category),
            ("severity", severity),
            ("operation_id", operation_id),
        ):
            if value is not None:
                filters.append(getattr(DiagnosticORM, name) == value)
        if since is not None:
            filters.append(DiagnosticORM.last_seen_at >= since)
        return filters

    async def query(
        self,
        *,
        since=None,
        agent_name=None,
        category=None,
        severity=None,
        operation_id=None,
        limit=100,
        before_id=None,
    ) -> list[dict]:
        if not 1 <= limit <= 501:
            raise ValueError("Diagnostic limit must be between 1 and 501")
        stmt = select(DiagnosticORM).where(
            *self._where(
                since=since,
                agent_name=agent_name,
                category=category,
                severity=severity,
                operation_id=operation_id,
            )
        )
        if before_id is not None:
            stmt = stmt.where(DiagnosticORM.id < before_id)
        async with self._tx() as conn:
            result = await conn.execute(stmt.order_by(DiagnosticORM.id.desc()).limit(limit))
            return [_row(row) for row in result.mappings()]

    async def get(self, record_id: int) -> dict | None:
        async with self._tx() as conn:
            result = await conn.execute(select(DiagnosticORM).where(DiagnosticORM.id == record_id))
            row = result.mappings().first()
            return _row(row) if row is not None else None

    async def digest(self, *, since=None, agent_name=None, limit=20) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("Diagnostic group limit must be between 1 and 100")
        filters = self._where(since=since, agent_name=agent_name)
        keys = [
            DiagnosticORM.category,
            DiagnosticORM.code,
            DiagnosticORM.severity,
            DiagnosticORM.agent_name,
            DiagnosticORM.runtime,
            DiagnosticORM.repo,
            DiagnosticORM.issue_number,
        ]
        grouped = (
            select(
                *keys,
                func.min(DiagnosticORM.first_seen_at).label("first_seen_at"),
                func.max(DiagnosticORM.last_seen_at).label("last_seen_at"),
                func.sum(DiagnosticORM.occurrences).label("occurrences"),
                func.count(func.distinct(DiagnosticORM.operation_id)).label("operation_count"),
                func.max(DiagnosticORM.id).label("sample_id"),
            )
            .where(*filters, DiagnosticORM.severity.in_(("warning", "error")))
            .group_by(*keys)
        )
        group_rows = grouped.subquery()
        delivery_filters = []
        queue_filters = [MessageQueueORM.status.in_(("pending", "in_progress"))]
        if since is not None:
            delivery_filters.append(DeliveryORM.created_at >= since)
        if agent_name is not None:
            delivery_filters.append(DeliveryORM.session_name == agent_name)
            queue_filters.append(MessageQueueORM.session_name == agent_name)
        async with self._tx() as conn:
            totals = (
                await conn.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(group_rows.c.occurrences), 0),
                    ).select_from(group_rows)
                )
            ).one()
            groups = (
                (
                    await conn.execute(
                        select(group_rows)
                        .order_by(
                            group_rows.c.last_seen_at.desc(),
                            group_rows.c.sample_id.desc(),
                        )
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            earliest = (await conn.execute(select(func.min(DiagnosticORM.first_seen_at)))).scalar()
            info = (
                await conn.execute(
                    select(
                        DiagnosticORM.code,
                        func.sum(DiagnosticORM.occurrences),
                    )
                    .where(*filters, DiagnosticORM.severity == "info")
                    .group_by(DiagnosticORM.code)
                )
            ).all()
            delivery_counts = (
                await conn.execute(
                    select(DeliveryORM.outcome, func.count())
                    .where(
                        *delivery_filters,
                    )
                    .group_by(DeliveryORM.outcome)
                )
            ).all()
            queue_counts = (
                await conn.execute(
                    select(
                        MessageQueueORM.status,
                        func.count(),
                        func.min(MessageQueueORM.enqueued_at),
                    )
                    .where(*queue_filters)
                    .group_by(MessageQueueORM.status)
                )
            ).all()
        queue = {"pending": 0, "in_progress": 0, "oldest_pending_at": None}
        for status, count, oldest in queue_counts:
            queue[status] = count
            if status == "pending":
                queue["oldest_pending_at"] = oldest
        return {
            "since": since,
            "generated_at": datetime.now(UTC).isoformat(),
            "groups": [dict(row) for row in groups],
            "total_groups": totals[0],
            "total_occurrences": totals[1],
            "has_more": totals[0] > limit,
            "count_semantics": (
                "retained occurrences for operation/code records last seen in interval"
            ),
            "coverage": {
                "earliest_retained_at": earliest,
                "retention_days": None,
                "write_failures_since_process_start": type(self).write_failures,
            },
            "informational": dict(info),
            "deliveries": {
                "attempts": sum(count for _, count in delivery_counts),
                "outcomes": dict(delivery_counts),
            },
            "queue": queue,
        }

    async def prune(self, retention_days: int = 30) -> int:
        async with self._tx() as conn:
            result = await conn.execute(
                delete(DiagnosticORM).where(
                    DiagnosticORM.last_seen_at < cutoff_iso(days=retention_days),
                )
            )
            return result.rowcount
