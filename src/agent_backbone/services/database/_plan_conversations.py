"""Current-plan conversation metadata; never a store of historical checklists."""

from __future__ import annotations

import json
import re

from sqlalchemy import insert, select

from agent_backbone.services.database._time import now_iso
from agent_backbone.services.database.models import ConversationEntryORM, CurrentPlanORM

_E = ConversationEntryORM.__table__
_P = CurrentPlanORM.__table__


async def record_revision(conn, row) -> None:
    """Record a publication, or the current pre-upgrade publication as a baseline."""
    previous = await conn.scalar(
        select(_E.c.detail)
        .where(
            _E.c.kind == "plan", _E.c.artifact_id == row["author_id"], _E.c.entry_kind == "revision"
        )
        .order_by(_E.c.id.desc())
        .limit(1)
    )
    if previous and json.loads(previous)["revision_id"] == row["revision_id"]:
        return
    await conn.execute(
        insert(_E).values(
            kind="plan",
            artifact_id=row["author_id"],
            entry_kind="revision",
            created_at=now_iso(),
            author_type="agent",
            author_id=row["author_id"],
            author_name=row["author_name"],
            resulting_revision_id=row["revision_id"],
            detail=json.dumps(
                {
                    "revision_id": row["revision_id"],
                    "label": f"rev {row['revision_id']}",
                    "plan_id": row["plan_id"],
                    "published_at": row["updated_at"],
                }
            ),
        )
    )


async def published(conn, previous, current) -> None:
    if previous:
        await record_revision(conn, previous)
    await record_revision(conn, current)
    if not previous or previous["plan_id"] != current["plan_id"]:
        return
    old = {item["item_id"]: item for item in json.loads(previous["content"])["items"]}
    for item in json.loads(current["content"])["items"]:
        before = old.get(item["item_id"])
        if before and before["status"] != item["status"]:
            await conn.execute(
                insert(_E).values(
                    kind="plan",
                    artifact_id=current["author_id"],
                    entry_kind="step",
                    created_at=now_iso(),
                    author_type="agent",
                    author_id=current["author_id"],
                    author_name=current["author_name"],
                    detail=json.dumps(
                        {
                            "item_id": item["item_id"],
                            "title": item["text"],
                            "status": item["status"],
                            "section": item["section"],
                            "revision_id": current["revision_id"],
                            "plan_id": current["plan_id"],
                        }
                    ),
                )
            )


def _revisions(author_id):
    return select(_E).where(
        _E.c.kind == "plan", _E.c.artifact_id == author_id, _E.c.entry_kind == "revision"
    )


def _version(row, current):
    detail = json.loads(row["detail"])
    return {
        "revision_id": detail["revision_id"],
        "label": detail["label"],
        "created_at": detail["published_at"],
        "author_id": row["artifact_id"],
        "author_name": row["author_name"],
        "plan_id": detail["plan_id"],
        "content_available": bool(current and current["revision_id"] == detail["revision_id"]),
        "sequence": row["id"],
    }


async def artifact(conn, author_id: str, revision_ids=()) -> dict | None:
    """Load the latest metadata and only the particular revisions a write references."""
    if not re.fullmatch(r"[0-9a-f]{32}", author_id):
        return None
    current = (await conn.execute(select(_P).where(_P.c.author_id == author_id))).mappings().first()
    latest = (
        (await conn.execute(_revisions(author_id).order_by(_E.c.id.desc()).limit(1)))
        .mappings()
        .first()
    )
    if current is None and latest is None:
        return None
    rows = {} if latest is None else {latest["id"]: latest}
    selected = set(revision_ids) - {None}
    if selected:
        for row in (
            await conn.execute(
                _revisions(author_id)
                .where(_E.c.resulting_revision_id.in_(selected))
                .limit(len(selected))
            )
        ).mappings():
            rows[row["id"]] = row
    versions = [_version(rows[key], current) for key in sorted(rows)]
    unrecorded = bool(
        current and (latest is None or latest["resulting_revision_id"] != current["revision_id"])
    )
    if unrecorded:
        versions.append(
            {
                "revision_id": current["revision_id"],
                "label": f"rev {current['revision_id']}",
                "created_at": current["updated_at"],
                "author_id": author_id,
                "author_name": current["author_name"],
                "plan_id": current["plan_id"],
                "content_available": True,
                "sequence": latest["id"] + 1 if latest else 1,
            }
        )
    plan = json.loads(current["content"]) if current else None
    return {
        "kind": "plan",
        "artifact_id": author_id,
        "title": " ".join(plan["title"].split())[:120] if plan else "Current plan",
        "versions": versions,
        "latest": versions[-1],
        "responsible_id": author_id,
        "responsible_name": versions[-1]["author_name"],
        "current": current,
        "plan": plan,
        "unrecorded": unrecorded,
    }


async def version_page(conn, artifact, before, limit):
    query = _revisions(artifact["artifact_id"])
    if before is not None:
        query = query.where(_E.c.id < before)
    rows = (await conn.execute(query.order_by(_E.c.id.desc()).limit(limit + 1))).mappings().all()
    versions = [_version(row, artifact["current"]) for row in rows]
    if artifact["unrecorded"] and (before is None or artifact["latest"]["sequence"] < before):
        versions.insert(0, artifact["latest"])
    return versions
