"""Transactional capture used by senders, queue and delivery repositories."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select, text

from agent_backbone.services.database._time import now_iso
from agent_backbone.services.database.models import AgentORM, InteractionEventORM, InteractionORM

_A = AgentORM.__table__
_I = InteractionORM.__table__
_E = InteractionEventORM.__table__


def owner() -> dict:
    return {
        "kind": "owner",
        "id": "owner",
        "name": "You",
        "name_at_capture": "You",
        "attribution": "operator_asserted",
        "retired": False,
    }


async def actor(
    conn, name: str, *, attribution: str = "unverified", registration: str | None = None
) -> dict:
    query = select(_A.c.name, _A.c.report_identity)
    query = (
        query.where(_A.c.report_identity == registration)
        if registration
        else query.where(_A.c.name == name)
    )
    row = (await conn.execute(query)).first() if name or registration else None
    return {
        "kind": "agent" if row else "unknown",
        "id": row.report_identity if row else None,
        "name": row.name if row else name or "sender not recorded",
        "name_at_capture": name,
        "attribution": attribution,
        "retired": False,
    }


async def capture(
    conn,
    *,
    operation_id: str,
    message: str,
    sender: str,
    recipient: str,
    kind: str = "message",
    source: str = "",
    metadata: dict | None = None,
) -> str:
    """Must commit before any external handoff; exact retry retains its original."""
    existing = await find_operation(conn, operation_id)
    if existing:
        return existing
    metadata = metadata or {}
    if metadata.get("retry_of") and (original := await find_operation(conn, metadata["retry_of"])):
        await event(
            conn,
            operation_id,
            "redelivery",
            source=source,
            time_basis="recorded",
            key="accepted:" + operation_id,
            interaction_id=original,
        )
        return original
    sent_by = metadata.get("sender") or await actor(conn, sender)
    sent_to = metadata.get("recipient") or await actor(conn, recipient, attribution="system")
    body = metadata.get("body", message)
    at = metadata.get("accepted_at") or now_iso()
    interaction_id = uuid.uuid4().hex
    await conn.execute(
        text(
            "INSERT INTO interaction_coverage (id,capture_started_at,retained_from) "
            "VALUES (1,:at,:at) ON CONFLICT (id) DO NOTHING"
        ),
        {"at": at},
    )
    # Operation uniqueness makes capture idempotent across independent processes.
    await conn.execute(
        text("""INSERT INTO interactions
        (id,operation_id,accepted_at,kind,sender,recipient,sender_id,recipient_id,
         original_target,body,body_format,preview,relations)
        VALUES (:id,:op,:at,:kind,:sender,:recipient,:sender_id,:recipient_id,
                :target,:body,:format,:preview,:relations)
        ON CONFLICT (operation_id) DO NOTHING"""),
        {
            "id": interaction_id,
            "op": operation_id,
            "at": at,
            "kind": kind,
            "sender": json.dumps(sent_by),
            "recipient": json.dumps(sent_to),
            "sender_id": sent_by.get("id"),
            "recipient_id": sent_to.get("id"),
            "target": metadata.get("original_target", recipient),
            "body": body,
            "format": metadata.get("format", "text"),
            "preview": body[:200],
            "relations": json.dumps(metadata.get("relations", [])),
        },
    )
    interaction_id = await conn.scalar(select(_I.c.id).where(_I.c.operation_id == operation_id))
    await event(
        conn,
        operation_id,
        "accepted",
        source=source,
        at=at,
        time_basis="recorded",
        key="accepted:" + operation_id,
    )
    return interaction_id


async def event(
    conn,
    operation_id: str | None,
    kind: str,
    *,
    outcome: str | None = None,
    source: str = "",
    at: str | None = None,
    time_basis: str = "observed",
    delivery_id: int | None = None,
    queue_id: int | None = None,
    key: str | None = None,
    interaction_id: str | None = None,
) -> None:
    if not operation_id:
        return
    interaction_id = interaction_id or await find_operation(conn, operation_id)
    if not interaction_id:  # Legacy attempt: absence does not manufacture an original.
        return
    await conn.execute(
        text("""INSERT INTO interaction_events
        (interaction_id,operation_id,event_key,kind,time,time_basis,outcome,source,delivery_id,queue_id)
        VALUES (:interaction,:operation,:key,:kind,:time,:basis,:outcome,:source,:delivery,:queue)
        ON CONFLICT (event_key) DO NOTHING"""),
        {
            "interaction": interaction_id,
            "operation": operation_id,
            "key": key or uuid.uuid4().hex,
            "kind": kind,
            "time": at or now_iso(),
            "basis": time_basis,
            "outcome": outcome,
            "source": source,
            "delivery": delivery_id,
            "queue": queue_id,
        },
    )


async def find_operation(conn, operation_id):
    original = await conn.scalar(select(_I.c.id).where(_I.c.operation_id == operation_id))
    return original or await conn.scalar(
        select(_E.c.interaction_id).where(_E.c.operation_id == operation_id).limit(1)
    )
