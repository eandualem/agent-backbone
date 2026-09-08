"""Durable, bounded authored reports with a stable identity across agent renames."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, delete, func, insert, or_, select, update

from agent_backbone.models import REPORTS_PER_HOUR, PublishReport, ReportQuery
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso
from agent_backbone.services.database.models import AgentORM, ReportORM

_A = AgentORM.__table__
_R = ReportORM.__table__
_MAX_ID = 2**63 - 1
STALE_REPORT_SECONDS = 24 * 3600


class ReportConflict(ValueError):
    """A reused request key changed meaning, or an unchanged update was resubmitted."""


class ReportRateLimit(ValueError):
    """An author exhausted the bounded publication allowance."""


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    snapshot: int = Field(ge=0, le=_MAX_ID)
    id: int = Field(ge=0, le=_MAX_ID)
    priority: int = Field(ge=0, le=5)
    name: str = Field(max_length=128, pattern=r"^[A-Za-z0-9_.-]*$")
    scope: str = Field(pattern=r"^[0-9a-f]{64}$")


def _scope(query: ReportQuery) -> str:
    values = query.model_dump(exclude={"limit", "cursor"})
    values["agents"] = sorted(set(values["agents"]))
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _decode(query: ReportQuery, key: bytes) -> _Cursor | None:
    if not query.cursor:
        return None
    try:
        encoded, signature = query.cursor.rsplit(".", 1)
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if not hmac.compare_digest(hmac.digest(key, raw, "sha256"), bytes.fromhex(signature)):
            raise ValueError
        cursor = _Cursor.model_validate_json(raw)
        if cursor.scope != _scope(query) or cursor.id > cursor.snapshot:
            raise ValueError
        return cursor
    except (ValueError, ValidationError) as exc:
        raise ValueError(
            "invalid cursor; keep the original filters or refresh the view after a service restart"
        ) from exc


def _encode(query: ReportQuery, snapshot: int, row: dict, key: bytes) -> str:
    cursor = _Cursor(
        snapshot=snapshot,
        id=row.get("id") or 0,
        priority=row.get("priority") if row.get("priority") is not None else 5,
        name=row.get("agent_name") or "",
        scope=_scope(query),
    )
    raw = cursor.model_dump_json().encode()
    return base64.urlsafe_b64encode(raw).decode() + "." + hmac.digest(key, raw, "sha256").hex()


def _record(row: dict) -> dict:
    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    age = max(0, int((datetime.now(UTC) - created).total_seconds()))
    return {
        key: row[key]
        for key in ("id", "author_id", "author_name", "request_id", "created_at", "source")
    } | {
        "agent_name": row.get("agent_name"),
        "report": json.loads(row["content"]),
        "age_seconds": age,
        "telegram_delivery": row.get("telegram_delivery", "not_requested"),
        "telegram_audio_delivery": row.get("telegram_audio_delivery", "not_requested"),
        "stale": age >= STALE_REPORT_SECONDS,
    }


class ReportRepo(Repo):
    def __init__(self, engine) -> None:
        super().__init__(engine)
        # Navigation is process-local; stored reports are durable. A dedicated
        # random key never goes in the database or to authenticated API clients.
        # Restarting the service expires cursors and asks readers to refresh.
        self._cursor_key = secrets.token_bytes(32)

    async def publish(self, publication: PublishReport) -> tuple[dict, bool]:
        # Revalidate even typed values: model_construct/copy must not bypass the
        # database boundary. All callers get the same character and link limits.
        publication = PublishReport.model_validate(publication.model_dump())
        content = json.dumps(publication.report.model_dump(), sort_keys=True, ensure_ascii=False)
        async with self._tx() as conn:
            # This acquires the author row lock on PostgreSQL, and the writer lock
            # on SQLite. Concurrent publications/forget cannot race the identity,
            # idempotency check, or per-author rate bound.
            author = (
                (
                    await conn.execute(
                        update(_A)
                        .where(_A.c.name == publication.agent)
                        .values(report_identity=func.coalesce(_A.c.report_identity, uuid4().hex))
                        .returning(_A.c.report_identity, _A.c.tags)
                    )
                )
                .mappings()
                .first()
            )
            if author is None:
                raise KeyError(publication.agent)
            identity = author["report_identity"]
            previous = (
                (
                    await conn.execute(
                        select(_R).where(
                            _R.c.author_id == identity, _R.c.request_id == publication.request_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["content"] != content:
                    raise ReportConflict(
                        "request_id already published different content; use a new key"
                    )
                return _record(dict(previous) | {"agent_name": publication.agent}), False
            latest = (
                await conn.execute(
                    select(_R.c.id, _R.c.content)
                    .where(_R.c.author_id == identity)
                    .order_by(_R.c.id.desc())
                    .limit(1)
                )
            ).first()
            if latest and latest.content == content:
                raise ReportConflict(
                    f"report is unchanged from report {latest.id}; publish after progress"
                )
            count = await conn.scalar(
                select(func.count())
                .select_from(_R)
                .where(_R.c.author_id == identity, _R.c.created_at >= cutoff_iso(hours=1))
            )
            if count >= REPORTS_PER_HOUR:
                raise ReportRateLimit(
                    f"maximum {REPORTS_PER_HOUR} reports per agent per rolling hour"
                )
            report = publication.report
            priority = (
                0
                if report.blockers.kind == "owner"
                else 1
                if report.status == "blocked"
                else {"active": 2, "complete": 3, "inactive": 4}[report.status]
            )
            tags = json.loads(author["tags"])
            member = any(t.startswith("swarm:") for t in tags) and "role:coordinator" not in tags
            row = (
                (
                    await conn.execute(
                        insert(_R)
                        .values(
                            author_id=identity,
                            author_name=publication.agent,
                            request_id=publication.request_id,
                            created_at=now_iso(),
                            source="api",
                            content=content,
                            priority=priority,
                            swarm_member=int(member),
                            telegram_delivery="pending",
                        )
                        .returning(*_R.c)
                    )
                )
                .mappings()
                .one()
            )
            return _record(dict(row) | {"agent_name": publication.agent}), True

    async def claim_telegram(self, *, audio: bool = False) -> dict | None:
        """Lease one due text/audio job. Each lane has independent retry state."""
        prefix = "telegram_audio_" if audio else "telegram_"
        state, retry, lease, attempts = (
            _R.c[prefix + k] for k in ("delivery", "retry_at", "lease", "attempts")
        )
        due = and_(state.in_(("pending", "sending")), retry <= now_iso())
        token = uuid4().hex
        async with self._tx() as conn:
            candidate = select(_R.c.id).where(due).order_by(_R.c.id).limit(1).scalar_subquery()
            row = (
                (
                    await conn.execute(
                        update(_R)
                        .where(_R.c.id == candidate, due)
                        .values(
                            {
                                state: "sending",
                                lease: token,
                                retry: cutoff_iso(minutes=-5),
                                attempts: attempts + 1,
                            }
                        )
                        .returning(*_R.c)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            name = await conn.scalar(
                select(_A.c.name).where(_A.c.report_identity == row["author_id"])
            )
            return {
                "record": _record(dict(row) | {"agent_name": name}),
                "lease": token,
                "attempts": row[prefix + "attempts"],
                "parts": json.loads(row[prefix + "parts"]),
                "text_parts": json.loads(row["telegram_parts"]),
            }

    async def checkpoint_telegram(
        self, report_id: int, lease: str, parts: dict, *, audio: bool = False
    ) -> bool:
        prefix = "telegram_audio_" if audio else "telegram_"
        async with self._tx() as conn:
            result = await conn.execute(
                update(_R)
                .where(
                    _R.c.id == report_id,
                    _R.c[prefix + "delivery"] == "sending",
                    _R.c[prefix + "lease"] == lease,
                )
                .values({_R.c[prefix + "parts"]: json.dumps(parts)})
            )
            return bool(result.rowcount)

    async def finish_telegram(
        self,
        report_id: int,
        lease: str,
        *,
        message_id: str | None,
        attempts: int,
        audio: bool = False,
        request_audio: bool = False,
    ) -> bool:
        """Acknowledge this lane only; text completion can enqueue optional audio."""
        prefix = "telegram_audio_" if audio else "telegram_"
        delay = min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7))
        values = {
            _R.c[prefix + "delivery"]: "sent" if message_id is not None else "pending",
            _R.c[prefix + "lease"]: None,
            _R.c[prefix + "retry_at"]: "" if message_id is not None else cutoff_iso(seconds=-delay),
        }
        if not audio:
            values[_R.c.telegram_message_id] = message_id
            if message_id is not None and request_audio:
                values[_R.c.telegram_audio_delivery] = "pending"
        async with self._tx() as conn:
            result = await conn.execute(
                update(_R)
                .where(
                    _R.c.id == report_id,
                    _R.c[prefix + "delivery"] == "sending",
                    _R.c[prefix + "lease"] == lease,
                )
                .values(values)
            )
            return bool(result.rowcount)

    async def get(self, report_id: int) -> dict | None:
        async with self._tx() as conn:
            row = (
                (
                    await conn.execute(
                        select(_R, _A.c.name.label("agent_name"))
                        .select_from(_R.outerjoin(_A, _A.c.report_identity == _R.c.author_id))
                        .where(_R.c.id == report_id)
                    )
                )
                .mappings()
                .first()
            )
            return _record(dict(row)) if row else None

    async def query(self, query: ReportQuery) -> dict:
        query = ReportQuery.model_validate(query.model_dump())
        cursor = _decode(query, self._cursor_key)
        async with self._tx() as conn:
            snapshot = (
                cursor.snapshot if cursor else (await conn.scalar(select(func.max(_R.c.id))) or 0)
            )
            if query.agents:
                known = set(
                    (
                        await conn.execute(select(_A.c.name).where(_A.c.name.in_(query.agents)))
                    ).scalars()
                )
                if missing := set(query.agents) - known:
                    raise ValueError("unknown agent(s): " + ", ".join(sorted(missing)))
            if query.history:
                statement = (
                    select(_R, _A.c.name.label("agent_name"))
                    .select_from(_R.outerjoin(_A, _A.c.report_identity == _R.c.author_id))
                    .where(_R.c.id <= snapshot)
                )
                if cursor:
                    statement = statement.where(_R.c.id < cursor.id)
                if query.author_id:
                    statement = statement.where(_R.c.author_id == query.author_id)
                if query.agents:
                    statement = statement.where(_A.c.name.in_(query.agents))
                if not query.members and not query.agents and not query.author_id:
                    statement = statement.where(_R.c.swarm_member == 0)
                statement = statement.order_by(_R.c.id.desc())
            else:
                latest = (
                    select(_R.c.author_id, func.max(_R.c.id).label("latest_id"))
                    .where(_R.c.id <= snapshot)
                    .group_by(_R.c.author_id)
                    .subquery()
                )
                priority = func.coalesce(_R.c.priority, 5)
                row_id = func.coalesce(_R.c.id, 0)
                statement = select(
                    _R, _A.c.name.label("agent_name"), _A.c.report_identity.label("current_author")
                ).select_from(
                    _A.outerjoin(latest, latest.c.author_id == _A.c.report_identity).outerjoin(
                        _R, _R.c.id == latest.c.latest_id
                    )
                )
                if query.agents:
                    statement = statement.where(_A.c.name.in_(query.agents))
                elif not query.members:
                    statement = statement.where(
                        or_(~_A.c.tags.like('%"swarm:%'), _A.c.tags.like('%"role:coordinator"%'))
                    )
                if cursor:
                    statement = statement.where(
                        or_(
                            priority > cursor.priority,
                            and_(priority == cursor.priority, row_id < cursor.id),
                            and_(
                                priority == cursor.priority,
                                row_id == cursor.id,
                                _A.c.name > cursor.name,
                            ),
                        )
                    )
                statement = statement.order_by(priority, row_id.desc(), _A.c.name)
            rows = [
                dict(r) for r in (await conn.execute(statement.limit(query.limit + 1))).mappings()
            ]
        has_more = len(rows) > query.limit
        rows = rows[: query.limit]
        return {
            "items": [
                {
                    "agent_name": row["agent_name"],
                    "author_id": row.get("author_id") or row.get("current_author"),
                    "record": _record(row) if row["id"] is not None else None,
                }
                for row in rows
            ],
            "history": query.history,
            "generated_at": now_iso(),
            "stale_after_seconds": STALE_REPORT_SECONDS,
            "has_more": has_more,
            "next_cursor": _encode(query, snapshot, rows[-1], self._cursor_key)
            if has_more
            else None,
        }

    async def prune(self, days: int) -> int:
        """Prune old history, keeping each author's last account (including inactive)."""
        latest = select(func.max(_R.c.id)).group_by(_R.c.author_id)
        async with self._tx() as conn:
            result = await conn.execute(
                delete(_R).where(_R.c.created_at < cutoff_iso(days=days), _R.c.id.not_in(latest))
            )
            return result.rowcount or 0
