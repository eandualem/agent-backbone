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
from sqlalchemy import and_, func, insert, or_, select, update

from agent_backbone.models import REPORTS_PER_HOUR, PublishReport, PublishResponse, ReportQuery
from agent_backbone.services.database._conversation_events import append_revision
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso
from agent_backbone.services.database.models import AgentORM, ReportORM

_A = AgentORM.__table__
_R = ReportORM.__table__
_MAX_ID = 2**63 - 1
STALE_REPORT_SECONDS = 24 * 3600


class ReportConflict(ValueError):
    """A reused request key changed meaning, or an unchanged update was resubmitted."""


class ReportForbidden(ValueError):
    """Swarm participants report through their repository agent, not to humans."""


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
    record = {
        key: row[key]
        for key in ("id", "author_id", "author_name", "request_id", "created_at", "source")
    } | {
        "agent_name": row.get("agent_name"),
        "age_seconds": age,
        "telegram_delivery": row.get("telegram_delivery", "not_requested"),
        "telegram_audio_delivery": row.get("telegram_audio_delivery", "not_requested"),
        "conversation_id": row.get("conversation_id", row["id"]),
    }
    if row.get("kind", "report") == "response":
        return record | {
            "response": json.loads(row["content"]),
            "supersedes_id": row.get("supersedes_id"),
            "superseded_by": row.get("superseded_by"),
        }
    return record | {"report": json.loads(row["content"]), "stale": age >= STALE_REPORT_SECONDS}


async def chain_roots(conn, ids: list[int]) -> dict[int, int]:
    """Each response's correction-chain root: its earliest retained predecessor,
    which is its conversation ID. A report has no chain and is its own root."""
    roots = {i: i for i in ids}
    if not ids:
        return roots
    lineage = (
        select(_R.c.id.label("start_id"), _R.c.id, _R.c.supersedes_id)
        .where(_R.c.id.in_(ids))
        .cte("response_lineage", recursive=True)
    )
    lineage = lineage.union(
        select(lineage.c.start_id, _R.c.id, _R.c.supersedes_id).join(
            _R, _R.c.id == lineage.c.supersedes_id
        )
    )
    # A correction can only reference an already-published predecessor. UNION
    # also terminates on a malformed cycle instead of looping in Python.
    rows = await conn.execute(
        select(lineage.c.start_id, func.min(lineage.c.id)).group_by(lineage.c.start_id)
    )
    roots.update(rows.all())
    return roots


async def chain_ids(conn, root: int) -> list[int]:
    """Every response in the chain that starts at ``root``, oldest first."""
    found, frontier = [root], [root]
    while frontier:
        frontier = list(
            (
                await conn.scalars(
                    select(_R.c.id).where(_R.c.kind == "response", _R.c.supersedes_id.in_(frontier))
                )
            ).all()
        )
        found.extend(frontier)
    return sorted(found)


class ReportRepo(Repo):
    def __init__(self, engine) -> None:
        super().__init__(engine)
        # Navigation is process-local; stored reports are durable. A dedicated
        # random key never goes in the database or to authenticated API clients.
        # Restarting the service expires cursors and asks readers to refresh.
        self._cursor_key = secrets.token_bytes(32)

    async def publish(self, publication: PublishReport | PublishResponse) -> tuple[dict, bool]:
        # Revalidate even typed values: model_construct/copy must not bypass the
        # database boundary. All callers get the same character and link limits.
        response = isinstance(publication, PublishResponse)
        model = PublishResponse if response else PublishReport
        publication = model.model_validate(publication.model_dump())
        kind = "response" if response else "report"
        payload = publication.response if response else publication.report
        supersedes = publication.supersedes_id if response else None
        content = json.dumps(payload.model_dump(), sort_keys=True, ensure_ascii=False)
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
            if any(tag.startswith("swarm:") for tag in json.loads(author["tags"])):
                raise ReportForbidden(
                    "Swarm participants cannot publish "
                    + ("responses" if response else "progress reports")
                    + "; send findings to "
                    "the coordinator, who reports to the repository agent"
                )
            identity = author["report_identity"]
            previous = (
                (
                    await conn.execute(
                        select(_R).where(
                            _R.c.author_id == identity,
                            _R.c.kind == kind,
                            _R.c.request_id == publication.request_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["content"] != content or previous["supersedes_id"] != supersedes:
                    raise ReportConflict(
                        "request_id already published different content; use a new key"
                    )
                record = dict(previous) | {"agent_name": publication.agent}
                if response:
                    record["superseded_by"] = await conn.scalar(
                        select(func.max(_R.c.id)).where(
                            _R.c.kind == "response", _R.c.supersedes_id == previous["id"]
                        )
                    )
                    record["conversation_id"] = (await chain_roots(conn, [previous["id"]]))[
                        previous["id"]
                    ]
                return _record(record), False
            if supersedes is not None:
                previous_author = await conn.scalar(
                    select(_R.c.author_id).where(_R.c.id == supersedes, _R.c.kind == "response")
                )
                if previous_author != identity:
                    raise ReportConflict(
                        "supersedes_id must name a retained response by this author"
                    )
            latest = (
                await conn.execute(
                    select(_R.c.id, _R.c.content, _R.c.supersedes_id)
                    .where(_R.c.author_id == identity, _R.c.kind == kind)
                    .order_by(_R.c.id.desc())
                    .limit(1)
                )
            ).first()
            if latest and latest.content == content and latest.supersedes_id == supersedes:
                raise ReportConflict(
                    f"{kind} is unchanged from {kind} {latest.id}; publish after "
                    + ("a change" if response else "progress")
                )
            count = await conn.scalar(
                select(func.count())
                .select_from(_R)
                .where(
                    _R.c.author_id == identity,
                    _R.c.kind == kind,
                    _R.c.created_at >= cutoff_iso(hours=1),
                )
            )
            if count >= REPORTS_PER_HOUR:
                raise ReportRateLimit(
                    f"maximum {REPORTS_PER_HOUR} {kind}s per agent per rolling hour"
                )
            report = None if response else publication.report
            priority = (
                2
                if response
                else 0
                if report.blockers.kind == "owner"
                else 1
                if report.status == "blocked"
                else {"active": 2, "complete": 3, "inactive": 4}[report.status]
            )
            row = (
                (
                    await conn.execute(
                        insert(_R)
                        .values(
                            kind=kind,
                            supersedes_id=supersedes,
                            author_id=identity,
                            author_name=publication.agent,
                            request_id=publication.request_id,
                            created_at=now_iso(),
                            source="api",
                            content=content,
                            priority=priority,
                            swarm_member=0,
                            telegram_delivery="pending",
                        )
                        .returning(*_R.c)
                    )
                )
                .mappings()
                .one()
            )
            root = (await chain_roots(conn, [row["id"]]))[row["id"]]
            if supersedes is not None:
                versions = await chain_ids(conn, root)
                await append_revision(
                    conn,
                    "response",
                    str(root),
                    revision_id=str(row["id"]),
                    label=f"rev {versions.index(row['id']) + 1}",
                    author_id=identity,
                    name=publication.agent,
                )
            record = dict(row) | {"agent_name": publication.agent, "conversation_id": root}
            return _record(record), True

    async def claim_telegram(
        self, *, audio: bool = False, include_reports: bool = True
    ) -> dict | None:
        """Lease one due text/audio job. Each lane has independent retry state."""
        prefix = "telegram_audio_" if audio else "telegram_"
        state, retry, lease, attempts = (
            _R.c[prefix + k] for k in ("delivery", "retry_at", "lease", "attempts")
        )
        due = and_(state.in_(("pending", "sending")), retry <= now_iso())
        if not include_reports:
            due = and_(due, _R.c.kind == "response")
        token = uuid4().hex
        async with self._tx() as conn:
            # Retire pre-upgrade jobs as well as reports whose author was removed
            # or became a swarm participant. Text and audio must obey the same gate.
            eligible = and_(
                _R.c.swarm_member == 0,
                select(_A.c.name)
                .where(
                    _A.c.report_identity == _R.c.author_id,
                    ~_A.c.tags.like('%"swarm:%'),
                )
                .exists(),
            )
            await conn.execute(
                update(_R)
                .where(state.in_(("pending", "sending")), ~eligible)
                .values({state: "not_requested", lease: None, retry: ""})
            )
            candidate = (
                select(_R.c.id).where(due, eligible).order_by(_R.c.id).limit(1).scalar_subquery()
            )
            row = (
                (
                    await conn.execute(
                        update(_R)
                        .where(_R.c.id == candidate, due, eligible)
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

    async def get(self, report_id: int, *, kind: str = "report") -> dict | None:
        async with self._tx() as conn:
            row = (
                (
                    await conn.execute(
                        select(_R, _A.c.name.label("agent_name"))
                        .select_from(_R.outerjoin(_A, _A.c.report_identity == _R.c.author_id))
                        .where(_R.c.id == report_id, _R.c.kind == kind)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            roots = await chain_roots(conn, [report_id])
            record = _record(dict(row) | {"conversation_id": roots[report_id]})
            if kind == "response":
                record["superseded_by"] = await conn.scalar(
                    select(func.max(_R.c.id)).where(
                        _R.c.kind == "response", _R.c.supersedes_id == report_id
                    )
                )
            return record

    async def responses(
        self, *, agent: str | None = None, before_id: int | None = None, limit: int = 20
    ) -> dict:
        """Retained responses, newest first; separate from the progress feed."""
        if not 1 <= limit <= 100 or (before_id is not None and before_id < 1):
            raise ValueError("limit must be 1–100 and before_id must be positive")
        newer = _R.alias("newer")
        superseded_by = (
            select(func.max(newer.c.id))
            .where(newer.c.kind == "response", newer.c.supersedes_id == _R.c.id)
            .correlate(_R)
            .scalar_subquery()
        )
        statement = (
            select(_R, _A.c.name.label("agent_name"), superseded_by.label("superseded_by"))
            .select_from(_R.outerjoin(_A, _A.c.report_identity == _R.c.author_id))
            .where(_R.c.kind == "response")
        )
        if agent is not None:
            statement = statement.where(_A.c.name == agent)
        if before_id is not None:
            statement = statement.where(_R.c.id < before_id)
        async with self._tx() as conn:
            rows = (
                (await conn.execute(statement.order_by(_R.c.id.desc()).limit(limit + 1)))
                .mappings()
                .all()
            )
            roots = await chain_roots(conn, [row["id"] for row in rows[:limit]])
        items = [_record(dict(row) | {"conversation_id": roots[row["id"]]}) for row in rows[:limit]]
        more = len(rows) > limit
        return {
            "items": items,
            "has_more": more,
            "next_before_id": items[-1]["id"] if more else None,
        }

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
                    .where(_R.c.id <= snapshot, _R.c.kind == "report")
                )
                if cursor:
                    statement = statement.where(_R.c.id < cursor.id)
                if query.author_id:
                    statement = statement.where(_R.c.author_id == query.author_id)
                if query.agents:
                    statement = statement.where(_A.c.name.in_(query.agents))
                if not query.members and not query.agents and not query.author_id:
                    statement = statement.where(
                        _R.c.swarm_member == 0,
                        or_(_A.c.name.is_(None), ~_A.c.tags.like('%"swarm:%')),
                    )
                statement = statement.order_by(_R.c.id.desc())
            else:
                latest = (
                    select(_R.c.id)
                    .where(
                        _R.c.author_id == _A.c.report_identity,
                        _R.c.id <= snapshot,
                        _R.c.kind == "report",
                    )
                    .order_by(_R.c.id.desc())
                    .limit(1)
                    .correlate(_A)
                    .scalar_subquery()
                )
                priority = func.coalesce(_R.c.priority, 5)
                row_id = func.coalesce(_R.c.id, 0)
                statement = select(
                    _R, _A.c.name.label("agent_name"), _A.c.report_identity.label("current_author")
                ).select_from(_A.outerjoin(_R, _R.c.id == latest))
                if query.agents:
                    statement = statement.where(_A.c.name.in_(query.agents))
                elif not query.members:
                    statement = statement.where(~_A.c.tags.like('%"swarm:%'))
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
