"""Immutable Documents, independent of report retention and notification delivery."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import func, insert, select, text, update

from agent_backbone.models import (
    DOCUMENT_METADATA_BYTES,
    DOCUMENT_STORAGE_BYTES,
    DOCUMENTS_PER_AUTHOR_HOUR,
    DOCUMENTS_PER_HOUR,
    PublishDocument,
)
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso
from agent_backbone.services.database.models import AgentORM, DocumentORM, DocumentStorageORM

_A = AgentORM.__table__
_D = DocumentORM.__table__
_S = DocumentStorageORM.__table__
_METADATA = tuple(column for column in _D.c if column.name != "html")


class DocumentConflict(ValueError):
    """A retry changed content or a correction does not name the current owned revision."""


class DocumentRateLimit(ValueError):
    """New revisions exceeded a rolling-hour publication bound."""


class DocumentCapacity(ValueError):
    """Admission is full; retained content is never evicted to admit a publication."""


def _selection(*, body: bool = False):
    successor = _D.alias("successor")
    return select(
        *(_D.c if body else _METADATA),
        _A.c.name.label("agent_name"),
        select(successor.c.id)
        .where(successor.c.supersedes_id == _D.c.id)
        .scalar_subquery()
        .label("superseded_by"),
    ).select_from(_D.outerjoin(_A, _A.c.report_identity == _D.c.author_id))


class DocumentRepo(Repo):
    async def publish(self, publication: PublishDocument) -> tuple[dict, bool]:
        publication = PublishDocument.model_validate(publication.model_dump())
        async with self._tx() as conn:
            # Same identity as authored reports, retained in each revision after forget.
            # Updating first serializes publication with rename/forget on both backends.
            identity = await conn.scalar(
                update(_A)
                .where(_A.c.name == publication.author)
                .values(report_identity=func.coalesce(_A.c.report_identity, uuid4().hex))
                .returning(_A.c.report_identity)
            )
            if identity is None:
                raise KeyError(publication.author)
            previous = (
                (
                    await conn.execute(
                        select(_D).where(
                            _D.c.author_id == identity, _D.c.request_id == publication.request_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            # A correction can already have a successor: identical retries still succeed.
            if previous:
                if any(
                    previous[key]
                    != (
                        publication.html.encode("utf-8")
                        if key == "html"
                        else getattr(publication, key)
                    )
                    for key in ("title", "html", "supersedes_id")
                ):
                    raise DocumentConflict("request_id already published different content")
                record = await conn.execute(_selection().where(_D.c.id == previous["id"]))
                return dict(record.mappings().one()), False

            root = uuid4().hex
            if publication.supersedes_id is not None:
                predecessor = (
                    (await conn.execute(_selection().where(_D.c.id == publication.supersedes_id)))
                    .mappings()
                    .first()
                )
                if predecessor is None or predecessor["author_id"] != identity:
                    raise DocumentConflict("supersedes_id must name a document by this author")
                if predecessor["superseded_by"] is not None:
                    raise DocumentConflict("supersedes_id is stale; correct the current revision")
                root = predecessor["document_id"]

            # Global row lock makes aggregate rate/storage admission atomic across authors
            # and processes; no in-memory quota and no check-then-insert race.
            await conn.execute(
                text(
                    "INSERT INTO document_storage (id, used_bytes) VALUES (1, 0) "
                    "ON CONFLICT(id) DO NOTHING"
                )
            )
            used = await conn.scalar(
                update(_S)
                .where(_S.c.id == 1)
                .values(used_bytes=_S.c.used_bytes)
                .returning(_S.c.used_bytes)
            )
            recent = _D.c.created_at >= cutoff_iso(hours=1)
            total = await conn.scalar(select(func.count()).select_from(_D).where(recent))
            author_total = await conn.scalar(
                select(func.count()).select_from(_D).where(recent, _D.c.author_id == identity)
            )
            if total >= DOCUMENTS_PER_HOUR or author_total >= DOCUMENTS_PER_AUTHOR_HOUR:
                raise DocumentRateLimit(
                    "document publication allowance exceeded for this rolling hour"
                )
            raw = publication.html.encode("utf-8")
            # Account for both retained payload and bounded metadata, including retry data.
            admitted = len(raw) + DOCUMENT_METADATA_BYTES
            if used + admitted > DOCUMENT_STORAGE_BYTES:
                raise DocumentCapacity(
                    "document storage admission limit reached; retained documents are unchanged"
                )
            row_id = await conn.scalar(
                insert(_D)
                .values(
                    document_id=root,
                    author_id=identity,
                    author_name=publication.author,
                    request_id=publication.request_id,
                    title=publication.title,
                    html=raw,
                    supersedes_id=publication.supersedes_id,
                    created_at=now_iso(),
                    content_bytes=len(raw),
                    content_sha256=hashlib.sha256(raw).hexdigest(),
                )
                .returning(_D.c.id)
            )
            await conn.execute(update(_S).where(_S.c.id == 1).values(used_bytes=used + admitted))
            record = await conn.execute(_selection().where(_D.c.id == row_id))
            return dict(record.mappings().one()), True

    async def get(self, revision_id: int) -> dict | None:
        async with self._tx() as conn:
            row = (
                (await conn.execute(_selection(body=True).where(_D.c.id == revision_id)))
                .mappings()
                .first()
            )
            return dict(row) | {"html": row["html"].decode("utf-8")} if row else None

    async def query(self, *, agent=None, author_id=None, before_id=None, limit=20) -> dict:
        # Select latest BEFORE paging. Otherwise an old revision reappears below the cursor.
        latest = select(func.max(_D.c.id)).group_by(_D.c.document_id)
        query = _selection().where(_D.c.id.in_(latest))
        if agent is not None:
            query = query.where(_A.c.name == agent)
        if author_id is not None:
            query = query.where(_D.c.author_id == author_id)
        if before_id is not None:
            query = query.where(_D.c.id < before_id)
        async with self._tx() as conn:
            rows = (
                (await conn.execute(query.order_by(_D.c.id.desc()).limit(limit + 1)))
                .mappings()
                .all()
            )
        more = len(rows) > limit
        items = [dict(row) for row in rows[:limit]]
        return {
            "items": items,
            "has_more": more,
            "next_before_id": items[-1]["id"] if more else None,
        }
