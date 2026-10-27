"""Revision events, appended by each artifact's own publication in its transaction."""

from __future__ import annotations

import json

from sqlalchemy import insert

from agent_backbone.services.database._time import now_iso
from agent_backbone.services.database.models import ConversationEntryORM

_E = ConversationEntryORM.__table__


async def append_revision(
    conn, kind: str, artifact_id: str, *, revision_id: str, label: str, author_id: str, name: str
) -> None:
    """Record that a new version appeared and who published it. It never says
    which message caused it, and no conversation limit refuses it."""
    await conn.execute(
        insert(_E).values(
            kind=kind,
            artifact_id=artifact_id,
            entry_kind="revision",
            created_at=now_iso(),
            author_type="agent",
            author_id=author_id,
            author_name=name,
            detail=json.dumps({"revision_id": revision_id, "label": label}),
        )
    )
