"""Outbox retry ordering must let later events make progress."""

from unittest.mock import patch

import pytest
from sqlalchemy import text


@pytest.mark.parametrize("completed_status", ["delivered", "queued", "skipped"])
async def test_partial_fanouts_rotate_past_the_retry_window(db, completed_status):
    ids = []
    for number in range(21):
        event_id = await db.events.record(
            delivery_id=f"partial-{number}", source="poll", event_type="issue_opened"
        )
        ids.append(event_id)
        with patch(
            "agent_backbone.services.database._outbox_repo.now_iso",
            return_value=f"2000-01-01T00:00:{number:02d}Z",
        ):
            await db.outbox.plan(event_id, [{"session_name": "ike"}, {"session_name": "leo"}])
            await db.outbox.set_status(event_id, "ike", completed_status)

    first = await db.outbox.pending_events()
    assert first == ids[:20]
    for event_id in first:
        await db.outbox.set_status(event_id, "leo", "failed")
    second = await db.outbox.pending_events()
    assert len(second) == 20
    assert second[0] == ids[-1]
    assert second != first


async def test_completed_receipts_remain_available_for_event_reconciliation(db):
    event_id = await db.events.record(
        delivery_id="completed", source="poll", event_type="issue_opened"
    )
    await db.outbox.plan(event_id, [{"session_name": "ike"}, {"session_name": "leo"}])
    await db.outbox.set_status(event_id, "ike", "delivered")
    await db.outbox.set_status(event_id, "leo", "queued")
    assert await db.outbox.pending_events() == [event_id]
    assert await db.outbox.finish_event(event_id, "reconciled")
    assert await db.outbox.pending_events() == []


async def test_equal_retry_timestamps_use_event_id_order_and_requested_limit(db):
    ids = []
    for number in range(3):
        event_id = await db.events.record(
            delivery_id=f"tie-{number}", source="poll", event_type="issue_opened"
        )
        ids.append(event_id)
        await db.outbox.plan(event_id, [{"session_name": "ike"}])
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE event_outbox SET updated_at = '2000-01-01T00:00:00Z'"))
    assert await db.outbox.pending_events(limit=2) == ids[:2]
