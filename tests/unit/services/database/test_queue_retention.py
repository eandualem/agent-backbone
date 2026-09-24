"""Completed queue retention never removes waiting or leased work."""

from unittest.mock import patch

import pytest
from sqlalchemy import text

from tests.support import queue_row


@pytest.mark.parametrize("status", ["pending", "in_progress", "delivered", "expired"])
@pytest.mark.parametrize(
    "completed",
    [
        None,
        "2026-01-01T00:00:00.000000Z",
        "2026-02-01T00:00:00.000000Z",
        "2026-03-01T00:00:00.000000Z",
    ],
)
async def test_only_terminal_rows_completed_before_cutoff_are_pruned(db, status, completed):
    result = await db.queue.enqueue(
        session_name="app", message="private old text", delivery_kind="direct_message"
    )
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE message_queue SET status=:status, delivered_at=:completed, "
                "enqueued_at='2020-01-01T00:00:00.000000Z' WHERE id=:id"
            ),
            {"id": result.id, "status": status, "completed": completed},
        )
    with patch(
        "agent_backbone.services.database._queue_repo.cutoff_iso",
        return_value="2026-02-01T00:00:00.000000Z",
    ) as cutoff:
        count = await db.queue.prune(17)
    cutoff.assert_called_once_with(days=17)
    removed = status in ("delivered", "expired") and completed == "2026-01-01T00:00:00.000000Z"
    assert count == int(removed)
    assert (await queue_row(db, result.id) is None) == removed
    assert await db.deliveries.query() == []


async def test_a_restart_continuation_never_expires(db):
    note = await db.queue.enqueue(
        session_name="app",
        message="[via:backbone from:app] carry on",
        delivery_kind="direct_message",
        source="agent-restart",
    )
    chat = await db.queue.enqueue(
        session_name="app", message="[via:backbone from:leo] hi", delivery_kind="direct_message"
    )
    async with db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_queue SET enqueued_at='2020-01-01T00:00:00.000000Z'")
        )
    expired = await db.queue.expire_pending(max_age_minutes=30)
    assert [row["id"] for row in expired] == [chat.id]
    assert (await queue_row(db, note.id))["status"] == "pending"


async def _age_all(db):
    async with db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_queue SET enqueued_at='2020-01-01T00:00:00.000000Z'")
        )


async def test_messages_held_behind_an_unconfirmed_delivery_do_not_expire(db):
    await db.queue.enqueue(
        session_name="app",
        message="pasted, not confirmed",
        delivery_kind="direct_message",
        uncertain=True,
    )
    held = await db.queue.enqueue(
        session_name="app", message="[via:backbone from:leo] next", delivery_kind="direct_message"
    )
    other = await db.queue.enqueue(
        session_name="web", message="[via:backbone from:leo] hi", delivery_kind="direct_message"
    )
    await _age_all(db)
    expired = await db.queue.expire_pending(max_age_minutes=30)
    assert [row["id"] for row in expired] == [other.id]
    assert (await queue_row(db, held.id))["status"] == "pending"


async def test_an_expiry_notice_never_expires(db):
    notice = await db.queue.enqueue(
        session_name="app",
        message="[via:backbone] 1 message expired",
        delivery_kind="direct_message",
        source="queue-expiry",
    )
    await _age_all(db)
    assert await db.queue.expire_pending(max_age_minutes=30) == []
    assert (await queue_row(db, notice.id))["status"] == "pending"


async def test_a_released_hold_gives_the_waiting_queue_a_full_window(db):
    hold = await db.queue.enqueue(
        session_name="app",
        message="pasted, not confirmed",
        delivery_kind="direct_message",
        uncertain=True,
    )
    held = await db.queue.enqueue(
        session_name="app", message="[via:backbone from:leo] next", delivery_kind="direct_message"
    )
    await _age_all(db)  # the hold lasted longer than the expiry
    token = f"{hold.id}:{hold.operation_id}"
    assert await db.queue.acknowledge_checkpoint("app", [token]) == [token]
    assert await db.queue.expire_pending(max_age_minutes=30) == []
    assert (await queue_row(db, held.id))["status"] == "pending"
    released = await db.deliveries.query(session_name="app")
    assert [d["source"] for d in released] == ["uncertain-acknowledged"]
    async with db.engine.begin() as conn:  # the fresh window has passed
        await conn.execute(text("UPDATE deliveries SET created_at='2020-01-01T00:00:00.000000Z'"))
    assert [row["id"] for row in await db.queue.expire_pending(max_age_minutes=30)] == [held.id]
