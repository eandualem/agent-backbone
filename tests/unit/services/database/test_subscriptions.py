"""Subscriptions in the database: the agent's list and the queue's batches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from agent_backbone.models import SUBSCRIPTION_KIND
from agent_backbone.services.database import now_iso
from agent_backbone.services.database._queue_repo import SUBSCRIPTION_BATCH_LIMIT
from tests.support import queue_row


async def _agent(db, name="desk"):
    await db.agents.upsert(
        name,
        dir="/tmp/desk",
        runtime="claude",
        model=None,
        repo="",
        tags=[],
        env={},
        description="",
        unattended=False,
    )


class TestAgentSubscriptions:
    async def test_add_list_and_remove(self, db):
        await _agent(db)
        first = await db.agents.add_subscription("desk", "gmail", "from:upwork.com", "high")
        second = await db.agents.add_subscription("desk", "gmail", "from:linkedin.com", "normal")
        # The same filter again only changes its priority; no second row.
        again = await db.agents.add_subscription("desk", "gmail", "from:upwork.com", "normal")
        assert again == first
        rows = {row["name"]: row for row in await db.agents.list()}
        assert rows["desk"]["subscriptions"] == [
            {"id": first, "source": "gmail", "filter": "from:upwork.com", "priority": "normal"},
            {"id": second, "source": "gmail", "filter": "from:linkedin.com", "priority": "normal"},
        ]
        assert await db.agents.remove_subscription("desk", first) is True
        assert await db.agents.remove_subscription("desk", first) is False
        assert await db.agents.remove_subscription("other", second) is False
        rows = {row["name"]: row for row in await db.agents.list()}
        assert [s["id"] for s in rows["desk"]["subscriptions"]] == [second]

    async def test_forget_removes_subscriptions(self, db):
        await _agent(db)
        await db.agents.add_subscription("desk", "gmail", "from:upwork.com", "high")
        await db.agents.delete("desk")
        async with db.engine.begin() as conn:
            count = await conn.execute(text("SELECT COUNT(*) FROM agent_subscriptions"))
        assert count.scalar_one() == 0


class TestSubscriptionBatches:
    async def test_later_events_append_to_the_open_batch(self, db):
        first = await db.queue.enqueue_subscription(
            session_name="desk", header="[via:gmail] mail", lines=["- a"], priority=0
        )
        second = await db.queue.enqueue_subscription(
            session_name="desk", header="[via:gmail] mail", lines=["- b", "- c"], priority=0
        )
        assert first.status == "inserted"
        assert second.status == "appended"
        assert second.id == first.id
        assert second.stored
        assert await db.queue.pending_count("desk") == 1
        assert (await queue_row(db, first.id))["message"] == "[via:gmail] mail\n- a\n- b\n- c"

    async def test_high_batches_are_their_own_rows_and_drain_first(self, db):
        normal = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- n"], priority=0
        )
        high = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- h"], priority=1
        )
        later = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- h2"], priority=1
        )
        assert normal.id != high.id and later.status == "inserted" and later.id != high.id
        rows = await db.queue.dequeue("desk")
        assert [row["id"] for row in rows] == [high.id, later.id, normal.id]
        assert rows[0]["priority"] == 1
        assert rows[0]["delivery_kind"] == SUBSCRIPTION_KIND

    async def test_a_leased_batch_is_left_alone(self, db):
        first = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a"], priority=0
        )
        await db.queue.dequeue("desk")
        second = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- b"], priority=0
        )
        assert second.status == "inserted"
        assert second.id != first.id
        assert (await queue_row(db, first.id))["message"] == "h\n- a"

    async def test_a_replayed_event_is_not_listed_twice(self, db):
        """A poll run again before its events were marked processed adds nothing new."""
        first = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a", "- b"], priority=0
        )
        again = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a", "- b"], priority=0
        )
        assert again.status == "already_queued" and again.id == first.id and again.stored
        await db.queue.dequeue("desk")  # a leased batch still holds its lines
        high = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- b", "- c"], priority=1
        )
        assert high.status == "inserted"
        assert (await queue_row(db, high.id))["message"] == "h\n- c"
        assert (await queue_row(db, first.id))["message"] == "h\n- a\n- b"

    async def test_only_recently_delivered_batches_are_searched_for_a_replay(self, db):
        """Recency counts from delivery: a batch that waited long is still recent once delivered."""
        waited = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a"], priority=0
        )
        old = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- b"], priority=1
        )
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE message_queue SET status = 'delivered', "
                    "enqueued_at = '2020-01-01T00:00:00.000000Z', delivered_at = :at WHERE id = :id"
                ),
                [
                    {"id": waited.id, "at": now_iso()},
                    {"id": old.id, "at": "2020-01-02T00:00:00.000000Z"},
                ],
            )
        again = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a"], priority=0
        )
        assert again.status == "already_queued" and again.id == waited.id
        later = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- b"], priority=0
        )
        assert later.status == "inserted" and later.id not in (waited.id, old.id)

    async def test_a_replay_names_its_own_batch_and_offers_a_waiting_high_one_again(self, db):
        """The process may stop before a high batch is offered; the replay offers it, once."""
        high = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a"], priority=1
        )
        await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- b"], priority=1
        )
        again = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a"], priority=1
        )
        assert again.status == "already_queued"
        assert (again.id, again.operation_id) == (high.id, high.operation_id)
        assert again.context_offers == ((high.id, "h\n- a"),)
        await db.queue.dequeue("desk")  # the drain has it now
        leased = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a"], priority=1
        )
        assert leased.status == "already_queued" and leased.context_offers == ()

    async def test_a_full_batch_opens_the_next_one(self, db):
        lines = [f"- {n}" for n in range(SUBSCRIPTION_BATCH_LIMIT + 3)]
        result = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=lines, priority=0
        )
        assert result.status == "inserted"
        later = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- more"], priority=0
        )
        assert later.status == "appended" and later.id == result.id
        rows = await db.queue.dequeue("desk")
        assert [len(row["message"].split("\n")) - 1 for row in rows] == [
            SUBSCRIPTION_BATCH_LIMIT,
            4,
        ]
        assert rows[1]["message"].split("\n")[-1] == "- more"

    async def test_batches_never_expire(self, db):
        batch = await db.queue.enqueue_subscription(
            session_name="desk", header="h", lines=["- a"], priority=0
        )
        await db.queue.enqueue(session_name="desk", message="chat", delivery_kind="direct_message")
        async with db.engine.begin() as conn:
            await conn.execute(
                text("UPDATE message_queue SET enqueued_at = :t"),
                {"t": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
            )
        expired = await db.queue.expire_pending(max_age_minutes=30)
        assert [row["delivery_kind"] for row in expired] == ["direct_message"]
        assert (await queue_row(db, batch.id))["status"] == "pending"
