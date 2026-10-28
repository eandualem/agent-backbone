"""Sheet conversation metadata and anchors into immutable stored revisions."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError
from sqlalchemy import insert, select

from agent_backbone.services.database._time import now_iso
from agent_backbone.services.database.models import ConversationEntryORM, SheetORM, SheetRevisionORM
from agent_backbone.sheets import SheetAnchor, SheetError

_E, _S, _R = (m.__table__ for m in (ConversationEntryORM, SheetORM, SheetRevisionORM))
_COLUMNS = tuple(
    _R.c[name]
    for name in ("seq", "revision_id", "editor_kind", "editor_id", "editor_name", "created_at")
)


def _version(row):
    return {
        "revision_id": row["revision_id"],
        "label": f"rev {row['revision_id']}",
        "created_at": row["created_at"],
        "author_id": row["editor_id"],
        "author_name": row["editor_name"],
        "sequence": row["seq"],
    }


async def artifact(conn, sheet_id, revision_ids=()):
    if not re.fullmatch(r"[0-9a-f]{32}", sheet_id):
        return None
    current = (
        (
            await conn.execute(
                select(_S.c.title, _S.c.author_id, _S.c.author_name, *_COLUMNS)
                .select_from(_S.join(_R, _R.c.revision_id == _S.c.current_revision_id))
                .where(_S.c.sheet_id == sheet_id, _R.c.sheet_id == sheet_id)
            )
        )
        .mappings()
        .first()
    )
    if current is None:
        return None
    rows = {current["seq"]: current}
    selected = set(revision_ids) - {None, current["revision_id"]}
    if selected:
        for row in (
            await conn.execute(
                select(*_COLUMNS)
                .where(
                    _R.c.sheet_id == sheet_id,
                    _R.c.revision_id.in_(selected),
                    _R.c.seq <= current["seq"],
                )
                .limit(len(selected))
            )
        ).mappings():
            rows[row["seq"]] = row
    return {
        "kind": "sheet",
        "artifact_id": sheet_id,
        "title": " ".join(current["title"].split())[:120],
        "versions": [_version(rows[key]) for key in sorted(rows)],
        "latest": _version(current),
        "responsible_id": current["author_id"],
        "responsible_name": current["author_name"],
    }


async def record_revision(conn, sheet_id, revision_id):
    """Caller holds the Sheet row lock; retries/baselines do not duplicate events."""
    if await conn.scalar(
        select(_E.c.id)
        .where(
            _E.c.kind == "sheet",
            _E.c.artifact_id == sheet_id,
            _E.c.entry_kind == "revision",
            _E.c.resulting_revision_id == revision_id,
        )
        .limit(1)
    ):
        return
    row = (
        (
            await conn.execute(
                select(*_COLUMNS).where(_R.c.sheet_id == sheet_id, _R.c.revision_id == revision_id)
            )
        )
        .mappings()
        .one()
    )
    await conn.execute(
        insert(_E).values(
            kind="sheet",
            artifact_id=sheet_id,
            entry_kind="revision",
            created_at=now_iso(),
            author_type="agent" if row["editor_kind"] == "agent" else "owner",
            author_id=row["editor_id"] or "",
            author_name=row["editor_name"],
            resulting_revision_id=revision_id,
            detail=json.dumps({"revision_id": revision_id, "label": f"rev {revision_id}"}),
        )
    )


async def validate_anchor(conn, artifact, anchor):
    """Validate selected IDs against observed content; later moves/deletions are safe."""
    if not anchor:
        return
    if anchor.get("item_id") is not None:
        raise SheetError("invalid_anchor", "A Sheet cannot have a plan item anchor.")
    selection = anchor.get("sheet")
    if selection is None:
        return
    if not anchor.get("revision_id"):
        raise SheetError("invalid_anchor", "A Sheet selection must name its observed revision.")
    try:
        selected = SheetAnchor.model_validate(selection)
    except ValidationError:
        raise SheetError(
            "invalid_anchor", "Select unique stable IDs for at most 100 cells."
        ) from None
    payload = await conn.scalar(
        select(_R.c.payload).where(
            _R.c.sheet_id == artifact["artifact_id"], _R.c.revision_id == anchor["revision_id"]
        )
    )
    if payload is None:
        raise SheetError("invalid_revision", "The anchor names no revision of this Sheet.")
    tab = next(
        (t for t in json.loads(payload)["workbook"]["tabs"] if t["tab_id"] == selected.tab_id), None
    )
    if (
        tab is None
        or not set(selected.row_ids).issubset(tab["row_ids"])
        or not set(selected.column_ids).issubset(tab["column_ids"])
    ):
        raise SheetError("invalid_anchor", "The selection is absent from the observed revision.")
