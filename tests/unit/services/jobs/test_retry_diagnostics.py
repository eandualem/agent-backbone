"""Caught retry failures remain visible, and only their own operation can recover them."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.jobs.retry import delivery_retry, drain_message_queue
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

_RETRY = "agent_backbone.services.jobs.retry"


async def test_dequeue_failure_groups_by_session_and_recovers_only_that_session(config, db):
    async def dequeue(session, **kwargs):
        if session == "ike":
            raise RuntimeError("private queue content")
        return []

    with patch.object(db.queue, "dequeue", side_effect=dequeue):
        for _ in range(2):
            await drain_message_queue(config, db, None, active_sessions={"ike", "leo"})
    records = await db.diagnostics.query(category="job")
    assert [row["code"] for row in records] == ["queue_drain_failed"]
    failed = records[0]
    assert failed["agent_name"] == "ike" and failed["occurrences"] == 2
    await drain_message_queue(config, db, None, active_sessions={"leo"})
    assert len(await db.diagnostics.query(category="job")) == 1
    await drain_message_queue(config, db, None, active_sessions={"ike"})
    records = await db.diagnostics.query(operation_id=failed["operation_id"])
    assert [row["code"] for row in records] == ["queue_drain_recovered", "queue_drain_failed"]
    assert records[0]["agent_name"] == "ike"


async def test_a_normal_blocked_queue_is_not_a_job_error(config, db):
    await db.queue.enqueue(session_name="ike", message="hello", delivery_kind="direct_message")
    with patch(
        "agent_backbone.services.routing._delivery.get_session_intelligence",
        return_value=SessionProfile(
            session_name="ike", intelligence=SessionIntelligence.AGENT_WORKING
        ),
    ):
        for _ in range(2):
            await drain_message_queue(config, db, None, active_sessions={"ike"})
    assert await db.diagnostics.query(category="job") == []
    assert await db.queue.pending_count("ike") == 1


@pytest.mark.parametrize(
    "repository, method, stage",
    [
        ("queue", "expire_stale_leases", "lease_recovery"),
        ("queue", "expire_pending", "queue_expiry"),
        ("deliveries", "reclaim_stale", "claim_reclaim"),
        ("outbox", "pending_events", "outbox_load"),
        ("deliveries", "failed", "issue_retry_read"),
    ],
)
async def test_caught_maintenance_failures_have_scoped_evidence(
    config, db, repository, method, stage
):
    with patch(f"{_RETRY}.list_sessions", return_value=[]):
        with patch.object(getattr(db, repository), method, side_effect=RuntimeError("unavailable")):
            await delivery_retry(config, db, AsyncMock())
        records = await db.diagnostics.query(category="job")
        (failed,) = [row for row in records if row["code"] == f"{stage}_failed"]
        assert failed["severity"] == "error"
        await delivery_retry(config, db, AsyncMock())
    same_operation = await db.diagnostics.query(operation_id=failed["operation_id"])
    assert {row["code"] for row in same_operation} == {f"{stage}_failed", f"{stage}_recovered"}


async def test_another_issue_in_the_same_batch_cannot_recover_failed_retry(config, db):
    for issue in (7, 8):
        await db.deliveries.record(
            repo="example/test",
            issue_number=issue,
            target_entity="ike",
            session_name="ike",
            outcome="offline",
        )

    async def retry(config, delivery, db, gh):
        if delivery["issue_number"] == 7:
            raise RuntimeError("failed issue 7")
        return "still_busy"

    with (
        patch(f"{_RETRY}.list_sessions", return_value=[]),
        patch(f"{_RETRY}.retry_delivery", side_effect=retry),
    ):
        await delivery_retry(config, db, AsyncMock())
    (failed,) = await db.diagnostics.query(category="job")
    assert failed["code"] == "issue_retry_failed"
    assert failed["issue_number"] == 7
    with (
        patch(f"{_RETRY}.list_sessions", return_value=[]),
        patch(f"{_RETRY}.retry_delivery", return_value="still_busy"),
    ):
        await delivery_retry(config, db, AsyncMock())
    same_operation = await db.diagnostics.query(operation_id=failed["operation_id"])
    assert {row["code"] for row in same_operation} == {
        "issue_retry_failed",
        "issue_retry_recovered",
    }
    assert {row["issue_number"] for row in same_operation} == {7}
