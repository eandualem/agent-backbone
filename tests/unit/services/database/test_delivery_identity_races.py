"""Stable receipt identities when legacy rows retire or a duplicate completes."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import text

from tests.support import queue_row


@pytest.mark.parametrize("operation", ["record", "delete", "rename"])
async def test_bound_acknowledgment_and_retirement_lock_registration_first(
    db, monkeypatch, operation
):
    await db.agents.upsert(
        "worker",
        dir="/tmp/example",
        runtime="codex",
        model=None,
        repo="acme/app",
        tags=[],
        env={},
        description="",
        unattended=False,
    )
    identity = (await db.agents.list())[0]["report_identity"]
    repository = db.acks if operation == "record" else db.agents
    original_tx = repository._tx
    locked = False

    @asynccontextmanager
    async def postgres_lock_order():
        async with original_tx() as conn:

            class Connection:
                dialect = SimpleNamespace(name="postgresql")

                async def execute(self, statement, params=None):
                    nonlocal locked
                    sql = str(statement)
                    if "FOR UPDATE" in sql:
                        assert "FROM agents WHERE" in sql
                        locked = True
                    if sql.startswith(("INSERT", "UPDATE", "DELETE")):
                        assert locked, "registration must be locked before dependent rows"
                    # SQLite runs the behavior; this spy checks PostgreSQL's lock order.
                    return await conn.execute(text(sql.replace(" FOR UPDATE", "")), params)

            yield Connection()

    monkeypatch.setattr(repository, "_tx", postgres_lock_order)
    if operation == "record":
        assert await db.acks.record(5, "worker", repo="acme/app", registration_id=identity)
    elif operation == "delete":
        assert await db.agents.delete("worker")
    else:
        await db.agents.rename("worker", "renamed")
    assert locked


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


async def test_an_issue_delivered_to_an_earlier_session_can_be_claimed_by_the_next(db):
    """#410, against the real unique index: a restart frees the earlier session's slot."""
    issue = {
        "issue_number": 7,
        "target_entity": "desk",
        "session_name": "desk",
        "source": "agent-monitor",
        "repo": "example/app",
    }
    common = {"dir": "/tmp/a", "runtime": "claude", "model": None, "repo": "", "tags": []}
    await db.agents.upsert("desk", description="", unattended=False, env={}, **common)
    earlier = await db.deliveries.claim(**issue)
    await db.deliveries.finalize(earlier, "delivered")
    assert await db.deliveries.claim(**issue) is None  # the same session: delivered once
    from agent_backbone.services.database import now_iso

    await db.agents.conversation_started("desk", now_iso())
    since = await db.agents.started_at("desk")
    again = await db.deliveries.claim(**issue, since=since)
    assert again is not None and again != earlier
    async with db.deliveries._tx() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, outcome, superseded_at FROM deliveries ORDER BY id")
            )
        ).all()
    assert rows[0][0] == earlier and rows[0][1] == "delivered" and rows[0][2]  # evidence kept
    assert await db.deliveries.claim(**issue, since=since) is None  # and once per session
