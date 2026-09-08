"""Stable receipt identities when legacy rows retire or a duplicate completes."""

from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from sqlalchemy import text

from tests.support import queue_row


@pytest.mark.parametrize("legacy", [True, False])
async def test_retirement_persists_the_operation_used_by_its_diagnostic(db, legacy):
    delivery_id = await db.deliveries.record(
        repo="example/test",
        issue_number=7,
        session_name="ike",
        target_entity="ike",
        outcome="offline",
    )
    original = (await db.deliveries.query())[0]["operation_id"]
    if legacy:
        async with db.deliveries._tx() as conn:
            await conn.execute(
                text("UPDATE deliveries SET operation_id = NULL WHERE id = :id"),
                {"id": delivery_id},
            )
    await db.deliveries.retire(delivery_id, "acknowledged")
    (retired,) = await db.deliveries.query()
    assert retired["outcome"] == "acknowledged"
    assert retired["operation_id"] is not None
    UUID(retired["operation_id"])
    if not legacy:
        assert retired["operation_id"] == original
    (diagnostic,) = await db.diagnostics.query(operation_id=retired["operation_id"])
    assert diagnostic["code"] == "retired_acknowledged"
    assert diagnostic["delivery_id"] == delivery_id
    await db.deliveries.retire(delivery_id, "issue_closed")
    assert (await db.deliveries.query())[0]["operation_id"] == retired["operation_id"]
    assert (await db.diagnostics.query(operation_id=retired["operation_id"]))[0]["occurrences"] == 1


@pytest.mark.parametrize("kind", ["issue", "direct_message"])
async def test_enqueue_retries_if_the_conflicting_row_completed_before_lookup(
    db, monkeypatch, kind
):
    fields = {
        "session_name": "ike",
        "message": "hello",
        "sender": "leo",
        "delivery_kind": kind,
        "repo": "example/test",
        "issue_number": 7 if kind == "issue" else None,
        "target_entity": "ike",
    }
    first = await db.queue.enqueue(**fields)
    original_tx = db.queue._tx
    completed = False

    @asynccontextmanager
    async def concurrent_completion():
        async with original_tx() as conn:

            class CompletingConnection:
                async def execute(self, statement, params):
                    nonlocal completed
                    if not completed and "RETURNING id, operation_id" in str(statement):
                        # Model the PostgreSQL READ COMMITTED interleaving:
                        # ON CONFLICT saw a row, which completed before lookup.
                        await conn.execute(
                            text("UPDATE message_queue SET status = 'delivered' WHERE id = :id"),
                            {"id": first.id},
                        )
                        completed = True
                    return await conn.execute(statement, params)

            yield CompletingConnection()

    monkeypatch.setattr(db.queue, "_tx", concurrent_completion)
    second = await db.queue.enqueue(**fields)
    assert completed
    assert second.status == "inserted"
    assert second.id != first.id
    assert second.operation_id != first.operation_id
    monkeypatch.setattr(db.queue, "_tx", original_tx)
    assert (await queue_row(db, first.id))["status"] == "delivered"
    active = await queue_row(db, second.id)
    assert active["status"] == "pending"
    assert active["operation_id"] == second.operation_id
    assert await db.queue.pending_count("ike") == 1
