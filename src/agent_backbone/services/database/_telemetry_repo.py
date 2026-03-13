"""Telemetry checkpoint repository."""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_backbone.services.database._repo_utils import row_to_dict, rows_to_dicts, utc_now_iso


def _encode_checkpoint(checkpoint: Mapping[str, object] | None) -> str:
    return json.dumps(checkpoint or {}, sort_keys=True, separators=(",", ":"))


def _decode_checkpoint(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def get_checkpoint(
    conn: AsyncConnection,
    session: str,
    source_ref: str,
) -> dict | None:
    """Fetch a telemetry checkpoint row."""
    result = await conn.execute(
        text(
            """SELECT * FROM telemetry_checkpoints
               WHERE session = :session AND source_ref = :source_ref"""
        ),
        {"session": session, "source_ref": source_ref},
    )
    payload = row_to_dict(result.fetchone())
    if payload is None:
        return None
    payload["checkpoint"] = _decode_checkpoint(payload.get("checkpoint"))
    return payload


async def query_checkpoints(
    conn: AsyncConnection,
    *,
    session: str | None = None,
    runtime: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """List telemetry checkpoints with optional filters."""
    query = "SELECT * FROM telemetry_checkpoints WHERE 1 = 1"
    params: dict[str, str | int] = {"lim": limit}

    if session is not None:
        query += " AND session = :session"
        params["session"] = session
    if runtime is not None:
        query += " AND runtime = :runtime"
        params["runtime"] = runtime

    query += " ORDER BY updated_at DESC LIMIT :lim"
    result = await conn.execute(text(query), params)
    rows = []
    for payload in rows_to_dicts(result.fetchall()):
        payload["checkpoint"] = _decode_checkpoint(payload.get("checkpoint"))
        rows.append(payload)
    return rows


async def upsert_checkpoint(
    conn: AsyncConnection,
    *,
    session: str,
    source_ref: str,
    runtime: str,
    source_kind: str,
    checkpoint: Mapping[str, object] | None,
    entity: str | None = None,
    last_event_ts: str | None = None,
) -> None:
    """Create or update a telemetry checkpoint row."""
    await conn.execute(
        text(
            """INSERT INTO telemetry_checkpoints (
                   session,
                   source_ref,
                   runtime,
                   source_kind,
                   entity,
                   checkpoint,
                   last_event_ts,
                   updated_at
               )
               VALUES (
                   :session,
                   :source_ref,
                   :runtime,
                   :source_kind,
                   :entity,
                   :checkpoint,
                   :last_event_ts,
                   :updated_at
               )
               ON CONFLICT(session, source_ref) DO UPDATE SET
                   runtime = excluded.runtime,
                   source_kind = excluded.source_kind,
                   entity = excluded.entity,
                   checkpoint = excluded.checkpoint,
                   last_event_ts = excluded.last_event_ts,
                   updated_at = excluded.updated_at"""
        ),
        {
            "session": session,
            "source_ref": source_ref,
            "runtime": runtime,
            "source_kind": source_kind,
            "entity": entity,
            "checkpoint": _encode_checkpoint(checkpoint),
            "last_event_ts": last_event_ts,
            "updated_at": utc_now_iso(),
        },
    )
