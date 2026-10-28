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
            detail=json.dumps(
                {
                    "revision_id": row["revision_id"],
                    "label": "Current plan",
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


async def artifact(conn, author_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", author_id):
        return None
    current = (await conn.execute(select(_P).where(_P.c.author_id == author_id))).mappings().first()
    rows = (
        (
            await conn.execute(
                select(_E)
                .where(
                    _E.c.kind == "plan",
                    _E.c.artifact_id == author_id,
                    _E.c.entry_kind == "revision",
                )
                .order_by(_E.c.id)
            )
        )
        .mappings()
        .all()
    )
    if current is None and not rows:
        return None
    versions = []
    for row in rows:
        detail = json.loads(row["detail"])
        versions.append(
            {
                "revision_id": detail["revision_id"],
                "label": detail["label"],
                "created_at": detail["published_at"],
                "author_id": author_id,
                "author_name": row["author_name"],
                "plan_id": detail["plan_id"],
                "content_available": bool(
                    current and current["revision_id"] == detail["revision_id"]
                ),
                "sequence": row["id"],
            }
        )
    if current and (not versions or versions[-1]["revision_id"] != current["revision_id"]):
        versions.append(
            {
                "revision_id": current["revision_id"],
                "label": "Current plan",
                "created_at": current["updated_at"],
                "author_id": author_id,
                "author_name": current["author_name"],
                "plan_id": current["plan_id"],
                "content_available": True,
                "sequence": versions[-1]["sequence"] + 1 if versions else 1,
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
    }
