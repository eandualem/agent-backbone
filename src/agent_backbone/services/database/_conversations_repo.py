"""Artifact conversations: append-only entries, stored before any delivery.

A conversation is keyed by ``(kind, artifact_id)`` and lasts across every
version of its artifact. Artifact content stays in its own store; entries only
reference versions. An owner message and its queue row are written in one
transaction, so a stored message always has exactly one delivery operation.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, and_, case, func, insert, not_, or_, select, text, update

from agent_backbone.services.database._conversation_events import append_revision
from agent_backbone.services.database._plan_conversations import artifact as plan_artifact
from agent_backbone.services.database._plan_conversations import record_revision, version_page
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._reports_repo import chain_ids, chain_roots
from agent_backbone.services.database._sheet_conversations import artifact as sheet_artifact
from agent_backbone.services.database._sheet_conversations import record_revision as sheet_revision
from agent_backbone.services.database._sheet_conversations import validate_anchor as sheet_anchor
from agent_backbone.services.database._time import cutoff_iso, format_iso, now_iso
from agent_backbone.services.database.models import (
    AgentORM,
    ConversationEntryORM,
    ConversationReadORM,
    ConversationRedeliveryORM,
    CurrentPlanORM,
    DiagnosticORM,
    DocumentORM,
    MessageQueueORM,
    ObservedSourceORM,
    ObservedVersionORM,
    ReportORM,
    SheetORM,
)
from agent_backbone.sheets import SheetError

_A = AgentORM.__table__
_D = DocumentORM.__table__
_DG = DiagnosticORM.__table__
_E = ConversationEntryORM.__table__
_P = CurrentPlanORM.__table__
_Q = MessageQueueORM.__table__
_R = ReportORM.__table__
_READ = ConversationReadORM.__table__
_REDELIVERY = ConversationRedeliveryORM.__table__
_S = ObservedSourceORM.__table__
_SHEET = SheetORM.__table__
_V = ObservedVersionORM.__table__

KINDS = ("document", "response", "report", "skill", "policy", "plan", "sheet")
"""Kinds with an adapter in this release."""
OBSERVED = ("skill", "policy")
"""Kinds whose versions are snapshots Backbone takes when it reads their source."""

TEXT_CHARACTERS = 4000
PAGE_BYTES = 1_048_576
_TITLE_CHARACTERS = 120

_DELIVERY = {
    "pending": "pending",
    "in_progress": "pending",
    "uncertain": "pending",
    "checkpoint": "held",
    "delivered": "delivered",
    "expired": "failed",
}
"""Queue status → the delivery state an owner message shows."""

Compose = Callable[[dict, dict], str]
"""Builds the delivered summary from the stored entry and its artifact."""


class ConversationRefused(ValueError):
    """A request the conversation does not accept, with its typed reason."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class UnknownArtifact(LookupError):
    """No artifact of this kind has this ID."""


@dataclass(frozen=True)
class Limits:
    max_messages: int
    owner_messages_per_hour: int


def _number(artifact_id: str) -> int | None:
    if re.fullmatch(r"[1-9][0-9]{0,18}", artifact_id) and int(artifact_id) <= 2**63 - 1:
        return int(artifact_id)
    return None


def _title(value: str) -> str:
    value = " ".join(value.split())
    return value if len(value) <= _TITLE_CHARACTERS else value[: _TITLE_CHARACTERS - 1] + "…"


async def _artifact(conn, kind: str, artifact_id: str, revision_ids=()) -> dict:
    """Its title and versions (oldest first), each with its author."""
    if kind == "sheet":
        if sheet := await sheet_artifact(conn, artifact_id, revision_ids):
            return sheet
        raise UnknownArtifact(f"No sheet {artifact_id}.")
    if kind == "plan":
        if plan := await plan_artifact(conn, artifact_id, revision_ids):
            return plan
        raise UnknownArtifact(f"No plan {artifact_id}.")
    if kind == "document":
        if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            raise UnknownArtifact(f"No document {artifact_id}.")
        rows = (
            await conn.execute(
                select(_D.c.id, _D.c.author_id, _D.c.author_name, _D.c.title, _D.c.created_at)
                .where(_D.c.document_id == artifact_id)
                .order_by(_D.c.id)
            )
        ).all()
        title = rows[-1].title if rows else ""
    elif kind in ("response", "report"):
        number = _number(artifact_id)
        ids = [number]
        if number and kind == "response":
            root = (await chain_roots(conn, [number]))[number]
            if root != number:
                raise UnknownArtifact(f"Response {number} belongs to conversation {root}.")
            ids = await chain_ids(conn, number)
        rows = (
            await conn.execute(
                select(_R.c.id, _R.c.author_id, _R.c.author_name, _R.c.content, _R.c.created_at)
                .where(_R.c.id.in_(ids), _R.c.kind == kind)
                .order_by(_R.c.id)
            )
        ).all()
        content = json.loads(rows[-1].content) if rows else {}
        title = content.get("question") or content.get("goal", {}).get("text", "")
    elif kind in OBSERVED:
        return await _observed(conn, kind, artifact_id)
    else:
        raise UnknownArtifact(f"No {kind} {artifact_id}.")
    if not rows:
        raise UnknownArtifact(f"No {kind} {artifact_id}.")
    versions = [
        {
            "revision_id": str(row.id),
            "label": f"rev {index}",
            "created_at": row.created_at,
            "author_id": row.author_id,
            "author_name": row.author_name,
        }
        for index, row in enumerate(rows, 1)
    ]
    return {
        "kind": kind,
        "artifact_id": artifact_id,
        "title": _title(title),
        "versions": versions,
        "latest": versions[-1],
        "responsible_id": versions[-1]["author_id"],
        "responsible_name": versions[-1]["author_name"],
    }


def _plan_anchor(artifact: dict, anchor: dict | None) -> dict | None:
    """Capture just selected context while the checked current row is locked."""
    if not anchor:
        return None
    if anchor.get("sheet") is not None:
        raise ConversationRefused(422, "invalid_anchor", "A plan cannot have a Sheet anchor.")
    if not anchor.get("revision_id"):
        raise ConversationRefused(
            422, "invalid_anchor", "A plan anchor must name the revision you observed."
        )
    current = artifact["current"]
    if current is None or anchor["revision_id"] != current["revision_id"]:
        raise ConversationRefused(
            409,
            "stale_revision",
            "This plan changed. Refresh and reselect the item before sending.",
        )
    plan = artifact["plan"]
    context = {"plan_id": plan["plan_id"], "title": plan["title"]}
    if item_id := anchor.get("item_id"):
        item = next((i for i in plan["items"] if i["item_id"] == item_id), None)
        if item is None:
            raise ConversationRefused(
                422, "invalid_anchor", "The item is not in the observed plan."
            )
        context["item"] = item
    return anchor | {"revision_id": current["revision_id"], "context": context}


async def _observed(conn, kind: str, artifact_id: str) -> dict:
    """A skill or policy: its snapshots are its versions, published by no agent,
    and only the operator's maintainer mapping makes an agent responsible."""
    source = None
    if re.fullmatch(r"[0-9a-f]{32}", artifact_id):
        source = (
            await conn.execute(select(_S).where(_S.c.artifact_id == artifact_id, _S.c.kind == kind))
        ).first()
    if source is None:
        raise UnknownArtifact(f"No {kind} {artifact_id}.")
    rows = (
        await conn.execute(
            select(_V.c.id, _V.c.sha256, _V.c.observed_at)
            .where(_V.c.artifact_id == artifact_id)
            .order_by(_V.c.id)
        )
    ).all()
    versions = [
        {
            "revision_id": str(row.id),
            "label": f"v{index}",
            "created_at": row.observed_at,
            "sha256": row.sha256,
            "author_id": None,
            "author_name": None,
        }
        for index, row in enumerate(rows, 1)
    ]
    return {
        "kind": kind,
        "artifact_id": artifact_id,
        "title": _title(source.name),
        "status": source.status,
        "versions": versions,
        "latest": versions[-1],
        "responsible_id": source.maintainer_id,
        "responsible_name": source.maintainer_name,
    }


async def _names(conn, ids) -> dict[str, str]:
    """Current names of the registrations still present."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = await conn.execute(
        select(_A.c.report_identity, _A.c.name).where(_A.c.report_identity.in_(ids))
    )
    return {row.report_identity: row.name for row in rows}


def _responsible(artifact: dict, names: dict[str, str]) -> dict:
    agent_id = artifact["responsible_id"]
    if agent_id is None:
        return {"agent_id": None, "name": None, "status": "unassigned"}
    return {
        "agent_id": agent_id,
        "name": names.get(agent_id, artifact["responsible_name"]),
        "status": "active" if agent_id in names else "retired",
    }


def _where(kind: str, artifact_id: str):
    return and_(_E.c.kind == kind, _E.c.artifact_id == artifact_id)


RETIRED = "retired_registration_changed"
"""The queue completes a row whose recipient registration changed without
delivering it; the reason is kept with the operation's diagnostics."""


def _entries():
    retired = (
        select(_DG.c.id)
        .where(_DG.c.operation_id == _E.c.operation_id, _DG.c.code == RETIRED)
        .exists()
    )
    return select(_E, _Q.c.status.label("queue_status"), retired.label("retired")).select_from(
        _E.outerjoin(_Q, _Q.c.operation_id == _E.c.operation_id)
    )


def _entry(row) -> dict:
    owner = row.author_type == "owner"
    entry = {
        "id": row.id,
        "kind": row.entry_kind,
        "created_at": row.created_at,
        "author": {
            "type": row.author_type,
            "agent_id": row.author_id or None,
            "name": row.author_name,
        },
    }
    if row.entry_kind != "message":
        return entry | {row.entry_kind: json.loads(row.detail)}
    entry |= {"text": row.text, "anchor": json.loads(row.anchor) if row.anchor else None}
    if owner:
        return entry | {
            "recipient": {"agent_id": row.recipient_id, "name": row.recipient_name},
            "delivery": "failed" if row.retired else _DELIVERY.get(row.queue_status, row.delivery),
        }
    return entry | {
        "in_reply_to": row.in_reply_to,
        "resulting_revision_id": row.resulting_revision_id,
    }


async def _identity(conn, name: str) -> str | None:
    """The registration ID of a registered agent, assigned on first use, as
    publishing a Document or Response does."""
    return await conn.scalar(
        update(_A)
        .where(_A.c.name == name)
        .values(report_identity=func.coalesce(_A.c.report_identity, uuid.uuid4().hex))
        .returning(_A.c.report_identity)
    )


async def _locked_name(conn, agent_id: str | None) -> str | None:
    """The current name of a registration, holding its row until the transaction
    ends, so a concurrent rename cannot strand a queue row under the old name."""
    if not agent_id:
        return None
    return await conn.scalar(
        update(_A)
        .where(_A.c.report_identity == agent_id)
        .values(name=_A.c.name)
        .returning(_A.c.name)
    )


async def _registration(conn, name: str) -> str | None:
    return await conn.scalar(select(_A.c.report_identity).where(_A.c.name == name))


async def _participant(conn, kind, artifact_id, artifact, agent_id: str) -> bool:
    if agent_id == artifact["responsible_id"]:
        return True
    return bool(
        await conn.scalar(
            select(_E.c.id)
            .where(
                _where(kind, artifact_id),
                or_(_E.c.author_id == agent_id, _E.c.recipient_id == agent_id),
            )
            .limit(1)
        )
    )


async def _lock(conn, kind: str, artifact_id: str) -> None:
    """Serialize writers of one conversation on its marker row, so retry keys,
    limits and redelivery are checked against a state no one else is changing."""
    await conn.execute(
        text(
            "INSERT INTO conversation_reads (kind, artifact_id, read_through, updated_at)"
            " VALUES (:kind, :artifact_id, 0, '') ON CONFLICT (kind, artifact_id) DO NOTHING"
        ),
        {"kind": kind, "artifact_id": artifact_id},
    )
    await conn.execute(
        update(_READ)
        .where(_READ.c.kind == kind, _READ.c.artifact_id == artifact_id)
        .values(read_through=_READ.c.read_through)
    )


async def _counts(conn, kind: str, artifact_id: str) -> dict:
    read_through = (
        await conn.scalar(
            select(_READ.c.read_through).where(
                _READ.c.kind == kind, _READ.c.artifact_id == artifact_id
            )
        )
        or 0
    )
    row = (
        await conn.execute(
            select(
                func.coalesce(func.sum(case((_E.c.entry_kind == "message", 1), else_=0)), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    _E.c.id > read_through,
                                    not_(
                                        and_(
                                            _E.c.entry_kind == "message",
                                            _E.c.author_type == "owner",
                                        )
                                    ),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.min(_E.c.id),
                func.max(_E.c.id),
            ).where(_where(kind, artifact_id))
        )
    ).one()
    return {
        "total": int(row[0]),
        "unread": int(row[1]),
        "read_through": read_through,
        "first_id": row[2],
        "last_id": row[3],
    }


async def _queue(
    conn,
    *,
    entry: dict,
    artifact: dict,
    recipient: tuple[str, str],
    compose: Compose,
    operation_id: str,
) -> None:
    """Insert one delivery operation for an owner message, bound to the
    registration that was checked: never re-resolved from the name, so a
    replacement agent with that name cannot receive it."""
    await conn.execute(
        insert(_Q).values(
            operation_id=operation_id,
            registration_id=recipient[0],
            session_name=recipient[1],
            message=compose(entry, artifact),
            repo="",
            issue_number=None,
            target_entity=None,
            delivery_kind="direct_message",
            source="conversation",
            enqueued_at=now_iso(),
            status="pending",
            sender="owner",
            dedup_key=f"src:conversation:{entry['id']}:{operation_id}",
            priority=0,
        )
    )


class ConversationRepo(Repo):
    async def artifact(self, kind: str, artifact_id: str) -> dict:
        async with self._tx() as conn:
            return await _artifact(conn, kind, artifact_id)

    async def conversation(
        self,
        kind: str,
        artifact_id: str,
        *,
        reader: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = 50,
        ids: list[int] | None = None,
    ) -> dict:
        """The header and one page of entries, oldest to newest within the page.
        ``reader`` is the agent asking; None is the owner. ``ids`` asks for those
        entries instead of a page, to refresh their delivery: an ID that is not in
        this conversation is left out, and no paging cursors are returned."""
        async with self._tx() as conn:
            artifact = await _artifact(conn, kind, artifact_id)
            if reader is not None:
                agent_id = await _registration(conn, reader)
                if not agent_id or not await _participant(
                    conn, kind, artifact_id, artifact, agent_id
                ):
                    raise ConversationRefused(
                        403, "not_participant", f"{reader} is not part of this conversation."
                    )
            counts = await _counts(conn, kind, artifact_id)
            query = _entries().where(_where(kind, artifact_id))
            if ids is not None:
                query = query.where(_E.c.id.in_(ids)).order_by(_E.c.id)
            elif after is not None:
                query = query.where(_E.c.id > after).order_by(_E.c.id)
            else:
                if before is not None:
                    query = query.where(_E.c.id < before)
                query = query.order_by(_E.c.id.desc())
            rows = (await conn.execute(query.limit(limit))).all()
            people = (
                await conn.execute(
                    select(_E.c.author_id, _E.c.author_name, _E.c.recipient_id, _E.c.recipient_name)
                    .where(_where(kind, artifact_id), _E.c.entry_kind == "message")
                    .distinct()
                )
            ).all()
            known = {}
            if artifact["responsible_id"]:
                known[artifact["responsible_id"]] = artifact["responsible_name"]
            for person in people:
                if person.author_id:
                    known.setdefault(person.author_id, person.author_name)
                if person.recipient_id:
                    known.setdefault(person.recipient_id, person.recipient_name)
            names = await _names(conn, known)
        entries = [_entry(row) for row in rows]
        while len(entries) > 1 and len(json.dumps(entries).encode()) > PAGE_BYTES:
            entries.pop()
        entries.sort(key=lambda entry: entry["id"])
        responsible = _responsible(artifact, names)
        source = {"source_status": artifact["status"]} if kind in OBSERVED else {}
        return {
            "kind": kind,
            "artifact_id": artifact_id,
            "title": artifact["title"],
            "latest_revision_id": artifact["latest"]["revision_id"],
            **source,
            "responsible": responsible,
            "participants": [
                {
                    "agent_id": agent_id,
                    "name": names.get(agent_id, name),
                    "status": "active" if agent_id in names else "retired",
                    "responsible": agent_id == responsible["agent_id"],
                }
                for agent_id, name in known.items()
            ],
            "total": counts["total"],
            "unread": counts["unread"],
            "read_through": counts["read_through"],
            "entries": entries,
            "older": f"e{entries[0]['id']}"
            if ids is None and entries and entries[0]["id"] > counts["first_id"]
            else None,
            "newer": f"e{entries[-1]['id']}"
            if ids is None and entries and entries[-1]["id"] < counts["last_id"]
            else None,
        }

    async def summaries(
        self,
        *,
        kind: str | None = None,
        keys: list[tuple[str, str]] = (),
        reader: str | None = None,
        after: int | None = None,
        since: tuple[int, int | None] | None = None,
        limit: int = 50,
    ) -> dict:
        """Conversations with at least one entry, most recently active first.
        ``since`` is a previous cursor: the last entry ID it saw and, when it has
        one, the time (µs since the epoch) from which read markers have moved."""
        last = func.max(_E.c.id)
        query = (
            select(
                _E.c.kind,
                _E.c.artifact_id,
                last.label("last_id"),
                func.max(_E.c.created_at).label("last_entry_at"),
                func.sum(case((_E.c.entry_kind == "message", 1), else_=0)).label("total"),
                func.sum(
                    case(
                        (
                            and_(
                                _E.c.id > func.coalesce(_READ.c.read_through, 0),
                                not_(
                                    and_(_E.c.entry_kind == "message", _E.c.author_type == "owner")
                                ),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("unread"),
            )
            .select_from(
                _E.outerjoin(
                    _READ, and_(_READ.c.kind == _E.c.kind, _READ.c.artifact_id == _E.c.artifact_id)
                )
            )
            .group_by(_E.c.kind, _E.c.artifact_id)
        )
        if kind is not None:
            query = query.where(_E.c.kind == kind)
        if keys:
            query = query.where(or_(*(_where(k, a) for k, a in keys)))
        if after is not None:
            query = query.having(last < after)
        if since is not None:
            changed = last > since[0]
            if since[1] is not None:
                moved = format_iso(datetime.fromtimestamp(since[1] / 1e6, UTC))
                changed = or_(changed, func.max(_READ.c.updated_at) >= moved)
            query = query.having(changed)
        # Read markers that move while this runs are reported again on the next poll.
        polled = int((datetime.now(UTC) - timedelta(seconds=2)).timestamp() * 1e6)
        async with self._tx() as conn:
            cursor = await conn.scalar(select(func.max(_E.c.id))) or 0
            if reader is not None:
                agent_id = await _registration(conn, reader)
                if agent_id is None:
                    return {"items": [], "next_after": None, "cursor": f"e{cursor}.{polled}"}
                mine = or_(_E.c.author_id == agent_id, _E.c.recipient_id == agent_id)
                latest = select(func.max(_D.c.id)).group_by(_D.c.document_id)
                documents = select(_D.c.document_id).where(
                    _D.c.author_id == agent_id, _D.c.id.in_(latest)
                )
                authored = select(func.cast(_R.c.id, Text)).where(_R.c.author_id == agent_id)
                maintained = select(_S.c.artifact_id).where(_S.c.maintainer_id == agent_id)
                responsible = or_(
                    and_(_E.c.kind == "document", _E.c.artifact_id.in_(documents)),
                    and_(_E.c.kind.in_(("response", "report")), _E.c.artifact_id.in_(authored)),
                    and_(_E.c.kind.in_(OBSERVED), _E.c.artifact_id.in_(maintained)),
                    and_(_E.c.kind == "plan", _E.c.artifact_id == agent_id),
                    and_(
                        _E.c.kind == "sheet",
                        _E.c.artifact_id.in_(
                            select(_SHEET.c.sheet_id).where(_SHEET.c.author_id == agent_id)
                        ),
                    ),
                )
                query = query.having(or_(func.sum(case((mine, 1), else_=0)) > 0, responsible))
            rows = (await conn.execute(query.order_by(last.desc()).limit(limit + 1))).all()
            items = []
            for row in rows[:limit]:
                try:
                    artifact = await _artifact(conn, row.kind, row.artifact_id)
                    names = await _names(conn, [artifact["responsible_id"]])
                    responsible = _responsible(artifact, names)
                except UnknownArtifact:
                    responsible = {"agent_id": None, "name": None, "status": "unassigned"}
                items.append(
                    {
                        "kind": row.kind,
                        "artifact_id": row.artifact_id,
                        "responsible": responsible,
                        "unread": int(row.unread),
                        "total": int(row.total),
                        "last_entry_at": row.last_entry_at,
                    }
                )
        more = len(rows) > limit
        return {
            "items": items,
            "next_after": f"c{rows[limit - 1].last_id}" if more else None,
            "cursor": f"e{cursor}.{polled}",
        }

    async def post(
        self,
        kind: str,
        artifact_id: str,
        *,
        author: str | None,
        request_id: str,
        text: str,
        recipient: str | None = None,
        anchor: dict | None = None,
        in_reply_to: int | None = None,
        resulting_revision_id: str | None = None,
        limits: Limits,
        compose: Compose,
    ) -> tuple[dict, bool, str | None]:
        """Append a message: ``(entry, created, session to deliver to)``.
        ``author`` None is the owner. Raises ConversationRefused, UnknownArtifact,
        or KeyError for an unregistered author."""
        digest = hashlib.sha256(
            json.dumps(
                [text, recipient, anchor, in_reply_to, resulting_revision_id], sort_keys=True
            ).encode()
        ).hexdigest()
        async with self._tx() as conn:
            artifact = await _artifact(conn, kind, artifact_id)
            # Assigning the identity takes the author's row lock first, as publishing does.
            author_id = "" if author is None else await _identity(conn, author)
            if author_id is None:
                raise KeyError(author)
            # A named recipient's row lock too comes before the conversation's,
            # in the same order as a reply; its refusal waits for the retry check.
            target = refused = None
            if author is None:
                try:
                    target = await self._recipient(conn, recipient, artifact)
                except ConversationRefused as exc:
                    refused = exc
            if kind == "plan":
                # Serialize against replacement/pruning after registration locks,
                # matching publication's registration -> current row order.
                await conn.execute(
                    update(_P)
                    .where(_P.c.author_id == artifact_id)
                    .values(revision_id=_P.c.revision_id)
                )
                artifact = await _artifact(
                    conn,
                    kind,
                    artifact_id,
                    ((anchor or {}).get("revision_id"), resulting_revision_id),
                )
            if kind == "sheet":
                # As in Sheet publication: registration before workbook, then conversation.
                await conn.execute(
                    update(_SHEET)
                    .where(_SHEET.c.sheet_id == artifact_id)
                    .values(current_revision_id=_SHEET.c.current_revision_id)
                )
                artifact = await _artifact(
                    conn,
                    kind,
                    artifact_id,
                    ((anchor or {}).get("revision_id"), resulting_revision_id),
                )
            await _lock(conn, kind, artifact_id)
            previous = (
                await conn.execute(
                    _entries().where(
                        _where(kind, artifact_id),
                        _E.c.author_id == author_id,
                        _E.c.request_id == request_id,
                    )
                )
            ).first()
            if previous is not None:
                if previous.request_sha256 != digest:
                    raise ConversationRefused(
                        409, "request_conflict", "This request_id was already used for a message."
                    )
                return _entry(previous), False, None
            if len(text) > TEXT_CHARACTERS:
                raise ConversationRefused(
                    422, "too_long", f"A message is at most {TEXT_CHARACTERS:,} characters."
                )
            counts = await _counts(conn, kind, artifact_id)
            if counts["total"] >= limits.max_messages:
                raise ConversationRefused(
                    409,
                    "conversation_full",
                    f"This conversation has reached {limits.max_messages:,} messages.",
                )
            versions = {v["revision_id"]: v for v in artifact["versions"]}
            if kind == "plan":
                anchor = _plan_anchor(artifact, anchor)
                if artifact["current"] is not None:
                    await record_revision(conn, artifact["current"])
            if kind == "sheet":
                try:
                    await sheet_anchor(conn, artifact, anchor)
                except SheetError as exc:
                    raise ConversationRefused(exc.status, exc.detail["code"], str(exc)) from None
                await sheet_revision(conn, artifact_id, artifact["latest"]["revision_id"])
            if anchor and anchor.get("revision_id") not in (None, *versions):
                raise ConversationRefused(
                    422, "invalid_revision", "The anchor names no version of this artifact."
                )
            if author is None:
                recent = await conn.scalar(
                    select(func.count()).where(
                        _where(kind, artifact_id),
                        _E.c.author_type == "owner",
                        _E.c.created_at >= cutoff_iso(hours=1),
                    )
                )
                if recent >= limits.owner_messages_per_hour:
                    raise ConversationRefused(
                        429,
                        "rate_limited",
                        f"At most {limits.owner_messages_per_hour} messages per conversation"
                        " per hour.",
                    )
                if refused is not None:
                    raise refused
            else:
                if not await _participant(conn, kind, artifact_id, artifact, author_id):
                    raise ConversationRefused(
                        403, "not_participant", f"{author} is not part of this conversation."
                    )
                if in_reply_to is not None and not await conn.scalar(
                    select(_E.c.id).where(_where(kind, artifact_id), _E.c.id == in_reply_to)
                ):
                    raise ConversationRefused(
                        422, "invalid_reply", "in_reply_to names no entry of this conversation."
                    )
                if resulting_revision_id is not None and (
                    resulting_revision_id not in versions
                    or versions[resulting_revision_id]["author_id"] != author_id
                ):
                    raise ConversationRefused(
                        422,
                        "invalid_revision",
                        "resulting_revision_id must be a version of this artifact you published.",
                    )
            entry_id = await conn.scalar(
                insert(_E)
                .values(
                    kind=kind,
                    artifact_id=artifact_id,
                    entry_kind="message",
                    created_at=now_iso(),
                    author_type="owner" if author is None else "agent",
                    author_id=author_id,
                    author_name="owner" if author is None else author,
                    text=text,
                    recipient_id=target[0] if target else None,
                    recipient_name=target[1] if target else None,
                    in_reply_to=in_reply_to,
                    resulting_revision_id=resulting_revision_id,
                    anchor=json.dumps(anchor) if anchor else None,
                    request_id=request_id,
                    request_sha256=digest,
                    delivery="pending" if target else None,
                )
                .returning(_E.c.id)
            )
            if target:
                entry = _entry((await conn.execute(_entries().where(_E.c.id == entry_id))).one())
                operation_id = uuid.uuid4().hex
                await _queue(
                    conn,
                    entry=entry,
                    artifact=artifact,
                    recipient=target,
                    compose=compose,
                    operation_id=operation_id,
                )
                await conn.execute(
                    update(_E).where(_E.c.id == entry_id).values(operation_id=operation_id)
                )
            row = (await conn.execute(_entries().where(_E.c.id == entry_id))).one()
            return _entry(row), True, target[1] if target else None

    async def _recipient(self, conn, recipient: str | None, artifact: dict) -> tuple[str, str]:
        """``(registration ID, current name)`` of the agent an owner message goes to.
        Never a new agent that merely has a retired registration's name."""
        if recipient is None:
            agent_id = artifact["responsible_id"]
            if name := await _locked_name(conn, agent_id):
                return agent_id, name
            raise ConversationRefused(
                422,
                "recipient_required",
                "The responsible agent is retired; name a recipient."
                if agent_id
                else "No agent is responsible for this; name a recipient.",
            )
        if re.fullmatch(r"[0-9a-f]{32}", recipient):
            if name := await _locked_name(conn, recipient):
                return recipient, name
        elif agent_id := await _identity(conn, recipient):
            return agent_id, recipient
        raise ConversationRefused(422, "unknown_recipient", f"No registered agent {recipient}.")

    async def mark_read(self, kind: str, artifact_id: str, read_through: int) -> dict:
        """Advance the owner's marker, never backwards nor past the last entry."""
        async with self._tx() as conn:
            await _artifact(conn, kind, artifact_id)
            last = await conn.scalar(select(func.max(_E.c.id)).where(_where(kind, artifact_id)))
            await conn.execute(
                text(
                    "INSERT INTO conversation_reads (kind, artifact_id, read_through, updated_at)"
                    " VALUES (:kind, :artifact_id, :read_through, :now)"
                    " ON CONFLICT (kind, artifact_id) DO UPDATE SET"
                    " updated_at = CASE"
                    " WHEN excluded.read_through > conversation_reads.read_through"
                    " THEN excluded.updated_at ELSE conversation_reads.updated_at END,"
                    " read_through = CASE"
                    " WHEN excluded.read_through > conversation_reads.read_through"
                    " THEN excluded.read_through ELSE conversation_reads.read_through END"
                ),
                {
                    "kind": kind,
                    "artifact_id": artifact_id,
                    "read_through": min(read_through, last or 0),
                    "now": now_iso(),
                },
            )
            counts = await _counts(conn, kind, artifact_id)
        return {key: counts[key] for key in ("read_through", "unread", "total")}

    async def redeliver(
        self, kind: str, artifact_id: str, entry_id: int, request_id: str, compose: Compose
    ) -> tuple[str, str | None, dict, str | None]:
        """Queue a new delivery for a failed owner message: ``(result, error,
        entry, session to deliver to)``. The same request_id returns its first result."""
        async with self._tx() as conn:
            artifact = await _artifact(conn, kind, artifact_id)
            query = _entries().where(
                _where(kind, artifact_id),
                _E.c.id == entry_id,
                _E.c.entry_kind == "message",
                _E.c.author_type == "owner",
            )
            # The recipient never changes on an entry: lock its registration first,
            # in the same order as sending, then the conversation, then re-read.
            recipient_id = await conn.scalar(select(_E.c.recipient_id).where(_E.c.id == entry_id))
            name = await _locked_name(conn, recipient_id)
            await _lock(conn, kind, artifact_id)
            row = (await conn.execute(query)).first()
            if row is None:
                raise ConversationRefused(404, "unknown_entry", f"No owner message {entry_id}.")
            prior = await conn.scalar(
                select(_REDELIVERY.c.result).where(
                    _REDELIVERY.c.entry_id == entry_id, _REDELIVERY.c.request_id == request_id
                )
            )
            if prior is not None:
                result, _, error = prior.partition(":")
                return result, error or None, _entry(row), None
            entry = _entry(row)
            target = None
            error = None
            if entry["delivery"] in ("pending", "held"):
                result = "already_pending"
            elif entry["delivery"] == "delivered":
                result, error = "refused", "already_delivered"
            elif name is None:
                result, error = "refused", "unknown_recipient"
            else:
                # Claim the failed operation atomically: a concurrent redelivery
                # that already replaced it finds nothing to claim.
                operation_id = uuid.uuid4().hex
                claimed = await conn.execute(
                    update(_E)
                    .where(_E.c.id == entry_id, _E.c.operation_id == row.operation_id)
                    .values(operation_id=operation_id, delivery="pending")
                )
                if claimed.rowcount:
                    target = name
                    await _queue(
                        conn,
                        entry=entry,
                        artifact=artifact,
                        recipient=(row.recipient_id, target),
                        compose=compose,
                        operation_id=operation_id,
                    )
                    result = "queued"
                else:
                    result = "already_pending"
                entry = _entry((await conn.execute(query)).one())
            await conn.execute(
                insert(_REDELIVERY).values(
                    entry_id=entry_id,
                    request_id=request_id,
                    result=f"{result}:{error}" if error else result,
                    created_at=now_iso(),
                )
            )
        return result, error, entry, target

    async def versions(
        self, kind: str, artifact_id: str, *, before: int | None = None, limit: int = 50
    ) -> dict:
        """Versions newest first; exact content stays with each kind's own read."""
        async with self._tx() as conn:
            artifact = await _artifact(conn, kind, artifact_id)
            candidates = (
                await version_page(conn, artifact, before, limit)
                if kind == "plan"
                else [
                    v
                    for v in reversed(artifact["versions"])
                    if before is None or int(v["revision_id"]) < before
                ]
            )
            names = await _names(conn, [v["author_id"] for v in candidates])
        versions = [v | {"agent_name": names.get(v["author_id"])} for v in candidates]
        more = len(versions) > limit
        items = versions[:limit]
        return {
            "items": items,
            "has_more": more,
            "next_before_id": (
                items[-1]["sequence"] if kind == "plan" else int(items[-1]["revision_id"])
            )
            if more
            else None,
        }

    async def observe(
        self, kind: str, store: str, items: dict[str, str | None], *, complete: bool
    ) -> dict[str, dict]:
        """Record what was just read from a skill or policy store: name → content,
        None when it could not be read. ``complete`` means ``items`` is the whole
        store, so a known item absent from it is missing too. A missing item keeps
        its identity; changed content becomes a new version and a revision event.
        Returns name → ``{artifact_id, status, latest_revision_id}``."""
        prefix = f"{store}/"
        result = {}
        async with self._tx() as conn:
            if complete:
                gone = [
                    row.artifact_id
                    for row in await conn.execute(
                        select(_S.c.artifact_id, _S.c.source_key).where(
                            _S.c.kind == kind, _S.c.status == "active"
                        )
                    )
                    # Names hold no "/": a nested store's keys are not this store's.
                    if row.source_key.startswith(prefix)
                    and "/" not in row.source_key[len(prefix) :]
                    and row.source_key[len(prefix) :] not in items
                ]
                if gone:
                    # Only still-active ones: a concurrent retirement must stand.
                    await conn.execute(
                        update(_S)
                        .where(_S.c.artifact_id.in_(gone), _S.c.status == "active")
                        .values(status="missing")
                    )
            for name, content in items.items():
                source = await _source(conn, kind, prefix + name, name, create=content is not None)
                if source is None:
                    continue
                digest = None if content is None else hashlib.sha256(content.encode()).hexdigest()
                status, latest = source.status, await _latest(conn, source.artifact_id)
                if (content is None and status == "active") or (
                    content is not None
                    and (status == "missing" or latest is None or latest[1] != digest)
                ):
                    status, latest = await _record(conn, kind, source.artifact_id, content, digest)
                result[name] = {
                    "artifact_id": source.artifact_id,
                    "status": status,
                    "latest_revision_id": str(latest[0]) if latest else None,
                }
        return result

    async def observed_source(self, kind: str, artifact_id: str) -> dict | None:
        """The identity's name, source key and status, or None."""
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    select(_S.c.name, _S.c.source_key, _S.c.status).where(
                        _S.c.artifact_id == artifact_id, _S.c.kind == kind
                    )
                )
            ).first()
        return dict(row._mapping) if row else None

    async def observed_version(self, kind: str, artifact_id: str, revision_id: str) -> dict:
        """One snapshot with its content."""
        async with self._tx() as conn:
            artifact = await _artifact(conn, kind, artifact_id)
            version = next(
                (v for v in artifact["versions"] if v["revision_id"] == revision_id), None
            )
            if version is None:
                raise UnknownArtifact(f"No version {revision_id} of {kind} {artifact_id}.")
            content = await conn.scalar(select(_V.c.content).where(_V.c.id == int(revision_id)))
        return {key: version[key] for key in ("revision_id", "label", "created_at", "sha256")} | {
            "content": content
        }

    async def set_maintainer(self, kind: str, artifact_id: str, agent: str | None) -> dict:
        """The operator maps a skill or policy to a registration, or clears it."""
        async with self._tx() as conn:
            await _observed(conn, kind, artifact_id)
            agent_id = name = None
            if agent is not None:
                if re.fullmatch(r"[0-9a-f]{32}", agent):
                    agent_id, name = agent, await _locked_name(conn, agent)
                else:
                    agent_id, name = await _identity(conn, agent), agent
                if not agent_id or not name:
                    raise ConversationRefused(
                        422, "unknown_recipient", f"No registered agent {agent}."
                    )
            await conn.execute(
                update(_S)
                .where(_S.c.artifact_id == artifact_id)
                .values(maintainer_id=agent_id, maintainer_name=name)
            )
            # The summary's responsible agent changed: report it to ``since`` polling.
            await _lock(conn, kind, artifact_id)
            await conn.execute(
                update(_READ)
                .where(_READ.c.kind == kind, _READ.c.artifact_id == artifact_id)
                .values(updated_at=now_iso())
            )
            artifact = await _observed(conn, kind, artifact_id)
            return _responsible(artifact, await _names(conn, [agent_id]))

    async def retire(self, kind: str, artifact_id: str) -> dict:
        """End an identity on the operator's word. Its conversation stays readable;
        anything later observed under the same name gets a new identity."""
        async with self._tx() as conn:
            await _observed(conn, kind, artifact_id)
            await conn.execute(
                update(_S)
                .where(_S.c.artifact_id == artifact_id, _S.c.status != "retired")
                .values(status="retired", retired_at=now_iso())
            )
            row = (
                await conn.execute(
                    select(_S.c.status, _S.c.retired_at).where(_S.c.artifact_id == artifact_id)
                )
            ).one()
        return {"artifact_id": artifact_id, "status": row.status, "retired_at": row.retired_at}


async def _source(conn, kind: str, key: str, name: str, *, create: bool):
    """The identity currently holding ``key``; a new one when ``create`` and
    there is none (never revived from a retired identity)."""
    query = (
        select(_S.c.artifact_id, _S.c.generation, _S.c.status)
        .where(_S.c.kind == kind, _S.c.source_key == key)
        .order_by(_S.c.generation.desc())
        .limit(1)
    )
    row = (await conn.execute(query)).first()
    if row is not None and row.status != "retired":
        return row
    if not create:
        return None
    # A concurrent first observation takes the same generation; the unique
    # index keeps one, and both read it back.
    await conn.execute(
        text(
            "INSERT INTO observed_sources (artifact_id, kind, source_key, generation, name,"
            " status, first_observed_at) VALUES (:artifact_id, :kind, :key, :generation,"
            " :name, 'active', :now) ON CONFLICT DO NOTHING"
        ),
        {
            "artifact_id": uuid.uuid4().hex,
            "kind": kind,
            "key": key,
            "generation": (row.generation if row else 0) + 1,
            "name": name,
            "now": now_iso(),
        },
    )
    return (await conn.execute(query)).first()


async def _latest(conn, artifact_id: str):
    """``(version ID, sha256)`` of the newest snapshot, or None."""
    return (
        await conn.execute(
            select(_V.c.id, _V.c.sha256)
            .where(_V.c.artifact_id == artifact_id)
            .order_by(_V.c.id.desc())
            .limit(1)
        )
    ).first()


async def _record(conn, kind: str, artifact_id: str, content: str | None, digest: str | None):
    """Apply one observation under the identity's row lock, so a concurrent
    observer of the same change finds this snapshot instead of adding another."""
    status = await conn.scalar(
        update(_S)
        .where(_S.c.artifact_id == artifact_id)
        .values(status=_S.c.status)
        .returning(_S.c.status)
    )
    latest = await _latest(conn, artifact_id)
    if status == "retired":
        return status, latest
    observed = "missing" if content is None else "active"
    if status != observed:
        await conn.execute(
            update(_S).where(_S.c.artifact_id == artifact_id).values(status=observed)
        )
    if content is None or (latest is not None and latest[1] == digest):
        return observed, latest
    version_id = await conn.scalar(
        insert(_V)
        .values(artifact_id=artifact_id, sha256=digest, content=content, observed_at=now_iso())
        .returning(_V.c.id)
    )
    if latest is not None:
        count = await conn.scalar(select(func.count()).where(_V.c.artifact_id == artifact_id))
        await append_revision(
            conn,
            kind,
            artifact_id,
            revision_id=str(version_id),
            label=f"v{count}",
            author_id="",
            name="backbone",
            author_type="backbone",
        )
    return observed, (version_id, digest)
