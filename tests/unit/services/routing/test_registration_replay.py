"""A retained notification belongs to one registration, never a reused name."""

import asyncio
from unittest.mock import patch

import pytest

from agent_backbone.config import AgentSpec
from agent_backbone.services.agents import AgentStore, lifecycle_lock
from agent_backbone.services.routing import safe_deliver
from tests.support import queue_row


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_forget_retires_pending_data_and_stale_copies_cannot_reach_reused_name(
    db, tmp_path, runtime
):
    store = AgentStore(db, tmp_path)
    await store.start()
    spec = AgentSpec(name="worker", dir=str(tmp_path), runtime=runtime)
    await store.register(spec)
    queued = await db.queue.enqueue(session_name="worker", message="old", delivery_kind="comment")
    (leased,) = await db.queue.dequeue("worker")
    identity = leased["registration_id"]
    assert identity
    await db.outbox.plan(1, [{"session_name": "worker", "target_entity": "worker"}])
    old_plan = (await db.outbox.entries(1))[0]["delivery"]
    assert old_plan["registration_id"] == identity
    await store.forget("worker")
    await store.register(spec)
    assert (await queue_row(db, queued.id))["status"] == "expired"
    history = await db.deliveries.query(operation_id=queued.operation_id)
    assert any(
        row["outcome"] == "expired" and row["source"] == "registration-retired" for row in history
    )
    assert (await db.outbox.entries(1))[0]["status"] == "skipped"
    assert not await db.agents.registration_matches("worker", identity)
    with patch("agent_backbone.services.routing._delivery.send_message") as send:
        result = await safe_deliver(
            "worker",
            "old",
            store.config,
            db=db,
            requeue=False,
            queue_id=queued.id,
            registration_id=identity,
        )
        assert result.outcome == "already_delivered"
        await safe_deliver(
            "worker", "old outbox", store.config, db=db, registration_id=old_plan["registration_id"]
        )
    send.assert_not_called()


async def test_delivery_waits_for_lifecycle_mutation_before_checking_registration(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    spec = AgentSpec(name="worker", dir=str(tmp_path), runtime="codex")
    await store.register(spec)
    queued = await db.queue.enqueue(session_name="worker", message="old", delivery_kind="comment")
    identity = (await queue_row(db, queued.id))["registration_id"]
    entered = asyncio.Event()

    async def attempt():
        entered.set()
        return await safe_deliver("worker", "old", store.config, db=db, registration_id=identity)

    with patch("agent_backbone.services.routing._delivery.send_message") as send:
        async with lifecycle_lock("worker"):
            task = asyncio.create_task(attempt())
            await entered.wait()
            await asyncio.sleep(0)
            assert not task.done()
            await store.forget("worker")
            await store.register(spec)
        assert (await task).outcome == "already_delivered"
    send.assert_not_called()


async def test_registration_identity_survives_configuration_updates_and_rename(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="worker", dir=str(tmp_path), runtime="codex"))
    identity = next(
        row["report_identity"] for row in await db.agents.list() if row["name"] == "worker"
    )
    await store.update("worker", description="new description")
    await db.agents.rename("worker", "renamed")
    assert await db.agents.registration_matches("renamed", identity)
    assert not await db.agents.registration_matches("worker", identity)


async def test_unknown_legacy_outbox_cannot_inherit_current_registration(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="worker", dir=str(tmp_path), runtime="codex"))
    with patch("agent_backbone.services.routing._delivery.send_message") as send:
        assert (
            await safe_deliver("worker", "legacy", store.config, db=db, registration_id=None)
        ).outcome == "already_delivered"
    send.assert_not_called()
