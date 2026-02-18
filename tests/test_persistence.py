"""Tests for src/persistence.py — all use in-memory SQLite."""

from __future__ import annotations

import pytest

from src.persistence import BackboneDB


@pytest.fixture
async def db():
    async with BackboneDB(":memory:") as database:
        yield database


class TestDeliveryTracking:
    async def test_record_and_query(self, db):
        row_id = await db.record_delivery(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            flow_name="issue-dispatcher",
        )
        assert row_id > 0

        results = await db.query_deliveries(issue_number=42)
        assert len(results) == 1
        assert results[0]["target_entity"] == "ike"
        assert results[0]["outcome"] == "delivered"

    async def test_query_by_entity(self, db):
        await db.record_delivery(42, "ike", "ike", "delivered")
        await db.record_delivery(43, "feynman", "feynman", "delivered")

        results = await db.query_deliveries(target_entity="ike")
        assert len(results) == 1
        assert results[0]["issue_number"] == 42

    async def test_query_by_outcome(self, db):
        await db.record_delivery(42, "ike", "ike", "delivered")
        await db.record_delivery(43, "feynman", "feynman", "offline")

        results = await db.query_deliveries(outcome="offline")
        assert len(results) == 1
        assert results[0]["issue_number"] == 43

    async def test_query_limit(self, db):
        for i in range(10):
            await db.record_delivery(i, "ike", "ike", "delivered")

        results = await db.query_deliveries(limit=3)
        assert len(results) == 3

    async def test_get_failed_deliveries(self, db):
        await db.record_delivery(1, "ike", "ike", "delivered")
        await db.record_delivery(2, "feynman", "feynman", "offline")
        await db.record_delivery(3, "leo", "leo", "delivery_failed")
        await db.record_delivery(4, "ada", "ada", "deferred")

        failed = await db.get_failed_deliveries()
        assert len(failed) == 3
        outcomes = {r["outcome"] for r in failed}
        assert outcomes == {"offline", "delivery_failed", "deferred"}

    async def test_prune_old_deliveries(self, db):
        # Insert a record, then prune with 0-day retention
        await db.record_delivery(42, "ike", "ike", "delivered")
        deleted = await db.prune_old_deliveries(retention_days=0)
        assert deleted == 1

        results = await db.query_deliveries()
        assert len(results) == 0


class TestDedupLog:
    async def test_first_delivery_not_duplicate(self, db):
        assert await db.is_duplicate_delivery_id("abc-123") is False

    async def test_recorded_delivery_is_duplicate(self, db):
        await db.record_delivery_id("abc-123")
        assert await db.is_duplicate_delivery_id("abc-123") is True

    async def test_empty_id_never_duplicate(self, db):
        assert await db.is_duplicate_delivery_id("") is False

    async def test_different_ids_not_duplicate(self, db):
        await db.record_delivery_id("abc-123")
        assert await db.is_duplicate_delivery_id("def-456") is False

    async def test_prune_delivery_ids(self, db):
        await db.record_delivery_id("old-1")
        # Prune with 0 hours retention — removes everything
        deleted = await db.prune_delivery_ids(max_age_hours=0)
        assert deleted == 1
        assert await db.is_duplicate_delivery_id("old-1") is False


class TestAgentState:
    async def test_set_and_get(self, db):
        await db.set_agent_state("ike", "idle")
        state = await db.get_agent_state("ike")
        assert state is not None
        assert state["state"] == "idle"
        assert state["session_name"] == "ike"

    async def test_get_nonexistent(self, db):
        state = await db.get_agent_state("nobody")
        assert state is None

    async def test_upsert(self, db):
        await db.set_agent_state("ike", "idle")
        await db.set_agent_state("ike", "processing_issue", current_issue=42)

        state = await db.get_agent_state("ike")
        assert state["state"] == "processing_issue"
        assert state["current_issue"] == 42

    async def test_get_all(self, db):
        await db.set_agent_state("ike", "idle")
        await db.set_agent_state("feynman", "busy")

        states = await db.get_all_agent_states()
        assert len(states) == 2
        names = {s["session_name"] for s in states}
        assert names == {"feynman", "ike"}

    async def test_preserves_last_activity_on_upsert(self, db):
        await db.set_agent_state("ike", "idle", last_activity="2025-01-01T00:00:00Z")
        # Update state without explicitly providing last_activity
        await db.set_agent_state("ike", "processing_issue")

        state = await db.get_agent_state("ike")
        assert state["last_activity"] == "2025-01-01T00:00:00Z"

    async def test_overrides_last_activity_when_provided(self, db):
        await db.set_agent_state("ike", "idle", last_activity="2025-01-01T00:00:00Z")
        await db.set_agent_state("ike", "idle", last_activity="2025-06-01T00:00:00Z")

        state = await db.get_agent_state("ike")
        assert state["last_activity"] == "2025-06-01T00:00:00Z"


class TestAcknowledgments:
    async def test_record_and_check_acknowledgment(self, db):
        await db.record_acknowledgment(42, "ike")
        assert await db.is_acknowledged(42, "ike") is True

    async def test_clear_acknowledgment(self, db):
        await db.record_acknowledgment(42, "ike")
        await db.clear_acknowledgment(42, "ike")
        assert await db.is_acknowledged(42, "ike") is False

    async def test_unacknowledged_by_default(self, db):
        assert await db.is_acknowledged(99, "nobody") is False


class TestMessageQueue:
    async def test_enqueue_and_dequeue(self, db):
        row_id = await db.enqueue_message(
            session_name="ike",
            message="Test message",
            issue_number=42,
            target_entity="ike",
            flow_name="test-flow",
        )
        assert row_id > 0

        messages = await db.dequeue_messages("ike")
        assert len(messages) == 1
        assert messages[0]["message"] == "Test message"
        assert messages[0]["issue_number"] == 42
        assert messages[0]["status"] == "pending"

    async def test_dequeue_empty(self, db):
        messages = await db.dequeue_messages("nobody")
        assert messages == []

    async def test_mark_delivered(self, db):
        row_id = await db.enqueue_message("ike", "msg")
        await db.mark_message_delivered(row_id)

        messages = await db.dequeue_messages("ike")
        assert messages == []  # delivered messages not returned

    async def test_dequeue_respects_limit(self, db):
        for i in range(5):
            await db.enqueue_message("ike", f"msg-{i}")

        messages = await db.dequeue_messages("ike", limit=2)
        assert len(messages) == 2

    async def test_dequeue_oldest_first(self, db):
        await db.enqueue_message("ike", "first")
        await db.enqueue_message("ike", "second")

        messages = await db.dequeue_messages("ike")
        assert messages[0]["message"] == "first"
        assert messages[1]["message"] == "second"

    async def test_dequeue_only_own_session(self, db):
        await db.enqueue_message("ike", "for ike")
        await db.enqueue_message("feynman", "for feynman")

        messages = await db.dequeue_messages("ike")
        assert len(messages) == 1
        assert messages[0]["session_name"] == "ike"

    async def test_enqueue_without_optional_fields(self, db):
        row_id = await db.enqueue_message("ike", "bare message")
        assert row_id > 0

        messages = await db.dequeue_messages("ike")
        assert len(messages) == 1
        assert messages[0]["issue_number"] is None
        assert messages[0]["target_entity"] is None

    async def test_mark_delivered_sets_timestamp(self, db):
        row_id = await db.enqueue_message("ike", "msg")
        await db.mark_message_delivered(row_id)

        # Query directly to check delivered_at is set
        cursor = await db.conn.execute(
            "SELECT delivered_at, status FROM message_queue WHERE id = ?", (row_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "delivered"
        assert row["delivered_at"] is not None

    async def test_multiple_sessions_independent(self, db):
        await db.enqueue_message("ike", "msg1", issue_number=1)
        await db.enqueue_message("feynman", "msg2", issue_number=2)
        await db.enqueue_message("ike", "msg3", issue_number=3)

        ike_msgs = await db.dequeue_messages("ike")
        feynman_msgs = await db.dequeue_messages("feynman")
        assert len(ike_msgs) == 2
        assert len(feynman_msgs) == 1
