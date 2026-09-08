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
