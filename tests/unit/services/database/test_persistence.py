"""Tests for agent_backbone/services/persistence — all use in-memory SQLite."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.services.database import BackboneDB


def _make_db() -> BackboneDB:
    """Create a BackboneDB with a lightweight engine for hot-cache-only tests."""
    return BackboneDB(create_async_engine("sqlite+aiosqlite:///:memory:"))


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    db = BackboneDB(engine)
    await db.start()
    try:
        yield db
    finally:
        db._engine = None
        await engine.dispose()


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

    async def test_get_failed_deliveries_includes_transient(self, db):
        """Transient outcomes, including busy-state deferrals, are retryable."""
        await db.record_delivery(1, "ike", "ike", "delivered")
        await db.record_delivery(2, "feynman", "feynman", "copy_mode")
        await db.record_delivery(3, "leo", "leo", "user_interacting")
        await db.record_delivery(4, "ada", "ada", "agent_working")
        await db.record_delivery(5, "brunel", "brunel", "plan_waiting")
        await db.record_delivery(6, "darwin", "darwin", "grace_period")

        failed = await db.get_failed_deliveries()
        assert len(failed) == 5
        outcomes = {r["outcome"] for r in failed}
        assert outcomes == {
            "agent_working",
            "copy_mode",
            "grace_period",
            "plan_waiting",
            "user_interacting",
        }
        # 'delivered' must not appear
        assert "delivered" not in outcomes

    async def test_get_failed_deliveries_excludes_superseded(self, db):
        """A failed delivery superseded by a later retried/delivered row is excluded."""
        await db.record_delivery(42, "ike", "ike", "offline")
        await db.record_delivery(42, "ike", "ike", "retried")

        failed = await db.get_failed_deliveries()
        assert len(failed) == 0

    async def test_get_failed_deliveries_excludes_superseded_by_delivered(self, db):
        """A failed delivery superseded by a later delivered row is excluded."""
        await db.record_delivery(42, "ike", "ike", "offline")
        await db.record_delivery(42, "ike", "ike", "delivered")

        failed = await db.get_failed_deliveries()
        assert len(failed) == 0

    async def test_get_failed_deliveries_includes_unsuperseded(self, db):
        """A failed delivery for entity A is not superseded by entity B's retried row."""
        await db.record_delivery(42, "coding-agent", "ike", "offline")
        await db.record_delivery(42, "feynman", "feynman", "retried")

        failed = await db.get_failed_deliveries()
        assert len(failed) == 1
        assert failed[0]["target_entity"] == "coding-agent"

    async def test_prune_old_deliveries(self, db):
        # Insert a record, then prune with 0-day retention
        await db.record_delivery(42, "ike", "ike", "delivered")
        deleted = await db.prune_old_deliveries(retention_days=0)
        assert deleted == 1

        results = await db.query_deliveries()
        assert len(results) == 0

    async def test_claim_delivery_attempt_success(self, db):
        claim_id = await db.claim_delivery_attempt(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            flow_name="issue-dispatcher",
        )

        assert isinstance(claim_id, int)
        rows = await db.query_deliveries(issue_number=42, session_name="ike")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "attempting"

    async def test_claim_delivery_attempt_conflict(self, db):
        first = await db.claim_delivery_attempt(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            flow_name="issue-dispatcher",
        )
        second = await db.claim_delivery_attempt(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            flow_name="issue-dispatcher",
        )

        assert isinstance(first, int)
        assert second is None

    async def test_finalize_delivery_attempt(self, db):
        claim_id = await db.claim_delivery_attempt(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            flow_name="issue-dispatcher",
        )

        await db.finalize_delivery_attempt(claim_id, "delivered")

        rows = await db.query_deliveries(issue_number=42, session_name="ike")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "delivered"

    async def test_reclaim_stale_attempts(self, db):
        claim_id = await db.claim_delivery_attempt(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            flow_name="issue-dispatcher",
        )

        async with db._engine.begin() as conn:
            await conn.execute(
                text("UPDATE deliveries SET created_at = :created_at WHERE id = :id"),
                {"created_at": "2000-01-01T00:00:00.000000Z", "id": claim_id},
            )

        reclaimed = await db.reclaim_stale_attempts(max_age_minutes=5)

        assert reclaimed == 1
        assert await db.query_deliveries(issue_number=42, session_name="ike") == []


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

    async def test_set_with_extended_fields(self, db):
        """New extended fields (entity, context, ts, plan_file, plan_title) are stored."""
        await db.set_agent_state(
            "feynman",
            "processing_issue",
            current_issue=571,
            entity="feynman",
            context="Phase 1 work",
            ts="1709500000.0",
            plan_file="/tmp/plan.md",
            plan_title="DB state migration",
        )
        state = await db.get_agent_state("feynman")
        assert state["entity"] == "feynman"
        assert state["context"] == "Phase 1 work"
        assert state["ts"] == "1709500000.0"
        assert state["plan_file"] == "/tmp/plan.md"
        assert state["plan_title"] == "DB state migration"

    async def test_extended_fields_override_when_provided(self, db):
        """Extended fields are overridden when explicitly provided."""
        await db.set_agent_state("ike", "idle", context="old context")
        await db.set_agent_state("ike", "busy", context="new context")

        state = await db.get_agent_state("ike")
        assert state["context"] == "new context"


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
            delivery_kind="comment",
            flow_name="test-flow",
        )
        assert row_id > 0

        messages = await db.dequeue_messages("ike")
        assert len(messages) == 1
        assert messages[0]["message"] == "Test message"
        assert messages[0]["issue_number"] == 42
        assert messages[0]["delivery_kind"] == "comment"
        assert messages[0]["status"] == "in_progress"
        assert messages[0]["leased_at"] is not None

    async def test_dequeue_empty(self, db):
        messages = await db.dequeue_messages("nobody")
        assert messages == []

    async def test_mark_delivered(self, db):
        row_id = await db.enqueue_message("ike", "msg")
        await db.dequeue_messages("ike")
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
        assert messages[0]["delivery_kind"] == "issue"
        assert messages[0]["issue_number"] is None
        assert messages[0]["target_entity"] is None

    async def test_mark_delivered_sets_timestamp(self, db):
        row_id = await db.enqueue_message("ike", "msg")
        await db.dequeue_messages("ike")
        await db.mark_message_delivered(row_id)

        # Use BackboneDB method to verify
        row = await db.get_message_by_id(row_id)
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

    async def test_enqueue_dedup_issue_constraint(self, db):
        first = await db.enqueue_message("ike", "first", issue_number=42, target_entity="ike")
        second = await db.enqueue_message("ike", "second", issue_number=42, target_entity="ike")

        assert first > 0
        assert second == -1

        messages = await db.dequeue_messages("ike")
        assert len(messages) == 1
        assert messages[0]["message"] == "first"

    async def test_enqueue_dedup_different_issues(self, db):
        first = await db.enqueue_message("ike", "first", issue_number=42, target_entity="ike")
        second = await db.enqueue_message("ike", "second", issue_number=43, target_entity="ike")

        assert first > 0
        assert second > 0

        messages = await db.dequeue_messages("ike")
        assert len(messages) == 2
        assert {message["issue_number"] for message in messages} == {42, 43}

    async def test_enqueue_dedup_comment_constraint(self, db):
        first = await db.enqueue_message(
            "ike",
            "same comment",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
        )
        duplicate = await db.enqueue_message(
            "ike",
            "same comment",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
        )
        different = await db.enqueue_message(
            "ike",
            "different comment",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
        )

        assert first > 0
        assert duplicate == -1
        assert different > 0

        messages = await db.dequeue_messages("ike")
        assert len(messages) == 2
        assert {message["message"] for message in messages} == {"same comment", "different comment"}

    async def test_enqueue_dedup_dm_constraint(self, db):
        first = await db.enqueue_message(
            "ike",
            "same direct message",
            delivery_kind="direct_message",
        )
        duplicate = await db.enqueue_message(
            "ike",
            "same direct message",
            delivery_kind="direct_message",
        )
        different = await db.enqueue_message(
            "ike",
            "different direct message",
            delivery_kind="direct_message",
        )

        assert first > 0
        assert duplicate == -1
        assert different > 0

        messages = await db.dequeue_messages("ike")
        assert len(messages) == 2
        assert {message["message"] for message in messages} == {
            "different direct message",
            "same direct message",
        }

    async def test_enqueue_content_hash_populated(self, db):
        message = "hash me"
        row_id = await db.enqueue_message(
            "ike",
            message,
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
        )

        row = await db.get_message_by_id(row_id)

        assert row["content_hash"] == hashlib.sha256(message.encode()).hexdigest()

    async def test_get_sessions_with_pending(self, db):
        await db.enqueue_message("ike", "pending one", issue_number=42, target_entity="ike")
        await db.enqueue_message("jarvis", "pending two", delivery_kind="direct_message")

        sessions = await db.get_sessions_with_pending()

        assert set(sessions) == {"ike", "jarvis"}

    async def test_dequeue_marks_in_progress(self, db):
        row_id = await db.enqueue_message("ike", "claim me", issue_number=42, target_entity="ike")

        messages = await db.dequeue_messages("ike")

        assert len(messages) == 1
        assert messages[0]["id"] == row_id
        assert messages[0]["status"] == "in_progress"
        assert messages[0]["leased_at"] is not None
        row = await db.get_message_by_id(row_id)
        assert row["status"] == "in_progress"
        assert row["leased_at"] is not None

    async def test_dequeue_skips_in_progress(self, db):
        await db.enqueue_message("ike", "claim me once", issue_number=42, target_entity="ike")

        first = await db.dequeue_messages("ike")
        second = await db.dequeue_messages("ike")

        assert len(first) == 1
        assert second == []

    async def test_release_lease(self, db):
        row_id = await db.enqueue_message("ike", "lease me", issue_number=42, target_entity="ike")
        await db.dequeue_messages("ike")

        await db.release_lease(row_id)

        row = await db.get_message_by_id(row_id)
        assert row["status"] == "pending"
        assert row["leased_at"] is None

    async def test_expire_stale_leases(self, db):
        row_id = await db.enqueue_message(
            "ike", "stale lease", issue_number=42, target_entity="ike"
        )
        await db.dequeue_messages("ike")

        async with db._engine.begin() as conn:
            await conn.execute(
                text("UPDATE message_queue SET leased_at = :leased_at WHERE id = :id"),
                {"leased_at": "2000-01-01T00:00:00.000000Z", "id": row_id},
            )

        expired = await db.expire_stale_leases(max_age_minutes=5)

        assert expired == 1
        row = await db.get_message_by_id(row_id)
        assert row["status"] == "pending"
        assert row["leased_at"] is None

    async def test_expire_stale_pending_exempts_direct_messages(self, db):
        """Direct messages survive the 30-minute expiry."""
        issue_id = await db.enqueue_message(
            "ike", "issue msg", issue_number=10, target_entity="ike"
        )
        dm_id = await db.enqueue_message("ike", "direct msg", delivery_kind="direct_message")

        # Backdate both to 1h ago (>30min but <24h)
        from datetime import UTC, datetime, timedelta

        one_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        async with db._engine.begin() as conn:
            await conn.execute(
                text("UPDATE message_queue SET enqueued_at = :ts WHERE id IN (:a, :b)"),
                {"ts": one_hour_ago, "a": issue_id, "b": dm_id},
            )

        expired = await db.expire_stale_pending(max_age_minutes=30)

        # Issue message expired; direct message survived
        assert expired >= 1
        assert (await db.get_message_by_id(issue_id))["status"] == "expired"
        assert (await db.get_message_by_id(dm_id))["status"] == "pending"

    async def test_expire_stale_pending_expires_old_direct_messages(self, db):
        """Direct messages older than 24h are expired."""
        dm_id = await db.enqueue_message("ike", "ancient dm", delivery_kind="direct_message")

        # Backdate to >24h ago
        async with db._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE message_queue SET enqueued_at = '2000-01-01T00:00:00.000000Z'"
                    " WHERE id = :id"
                ),
                {"id": dm_id},
            )

        expired = await db.expire_stale_pending(max_age_minutes=30)

        assert expired >= 1
        assert (await db.get_message_by_id(dm_id))["status"] == "expired"

    async def test_purge_covers_in_progress(self, db):
        first = await db.enqueue_message("ike", "claim me", issue_number=775, target_entity="ike")
        second = await db.enqueue_message(
            "feynman",
            "leave pending",
            issue_number=775,
            target_entity="feynman",
        )
        await db.dequeue_messages("ike")

        purged = await db.purge_pending_for_issue(775)

        assert purged == 2
        assert (await db.get_message_by_id(first))["status"] == "delivered"
        assert (await db.get_message_by_id(second))["status"] == "delivered"

    async def test_mark_delivered_requires_in_progress(self, db):
        row_id = await db.enqueue_message(
            "ike", "pending row", issue_number=42, target_entity="ike"
        )

        await db.mark_message_delivered(row_id)

        row = await db.get_message_by_id(row_id)
        assert row["status"] == "pending"
        assert row["delivered_at"] is None

    async def test_mark_matching_messages_delivered_clears_same_comment_identity(self, db):
        matching = await db.enqueue_message(
            "ike",
            "same comment",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
        )
        other_issue = await db.enqueue_message(
            "ike",
            "same comment",
            issue_number=43,
            target_entity="ike",
            delivery_kind="comment",
        )

        cleared = await db.mark_matching_messages_delivered(
            session_name="ike",
            message="same comment",
            delivery_kind="comment",
            issue_number=42,
        )

        assert cleared == 1
        assert (await db.get_message_by_id(matching))["status"] == "delivered"
        assert (await db.get_message_by_id(other_issue))["status"] == "pending"

    async def test_mark_matching_messages_delivered_clears_in_progress_direct_messages(self, db):
        row_id = await db.enqueue_message(
            "ike",
            "same direct message",
            delivery_kind="direct_message",
        )
        await db.dequeue_messages("ike")

        cleared = await db.mark_matching_messages_delivered(
            session_name="ike",
            message="same direct message",
            delivery_kind="direct_message",
        )

        assert cleared == 1
        row = await db.get_message_by_id(row_id)
        assert row["status"] == "delivered"
        assert row["leased_at"] is None


class TestDedupHotCache:
    def test_first_delivery_not_duplicate(self):
        db = _make_db()
        assert db.is_duplicate("abc-123") is False

    def test_duplicate_delivery(self):
        db = _make_db()
        db.is_duplicate("abc-123")
        assert db.is_duplicate("abc-123") is True

    def test_empty_id_never_duplicate(self):
        db = _make_db()
        assert db.is_duplicate("") is False

    def test_max_capacity_eviction(self):
        db = _make_db()
        for i in range(150):
            db.is_duplicate(f"delivery-{i}", max_ids=100)
        assert db.is_duplicate("delivery-0", max_ids=100) is False
        assert db.is_duplicate("delivery-149", max_ids=100) is True

    async def test_load_dedup_cache(self, db):
        """load_dedup_cache populates hot cache from database."""
        await db.record_delivery_id("cached-1")
        await db.record_delivery_id("cached-2")
        # Clear the hot cache to simulate cold start
        db._seen_deliveries.clear()
        await db.load_dedup_cache()
        assert db.is_duplicate("cached-1") is True
        assert db.is_duplicate("cached-2") is True
        assert db.is_duplicate("not-cached") is False


class TestAgentActivity:
    async def test_record_and_get(self, db):
        row_id = await db.record_activity("ike", "tool_use", '{"tool":"Edit"}', "1709500001.0")
        assert row_id > 0

        events = await db.get_activity("ike")
        assert len(events) == 1
        assert events[0]["session"] == "ike"
        assert events[0]["event"] == "tool_use"
        assert events[0]["data"] == '{"tool":"Edit"}'
        assert events[0]["ts"] == "1709500001.0"
        assert events[0]["received_at"] != ""

    async def test_get_empty(self, db):
        events = await db.get_activity("nobody")
        assert events == []

    async def test_get_respects_limit(self, db):
        for i in range(10):
            await db.record_activity("ike", "tool_use", None, str(1709500000 + i))

        events = await db.get_activity("ike", limit=3)
        assert len(events) == 3

    async def test_get_newest_first(self, db):
        await db.record_activity("ike", "first", None, "100.0")
        await db.record_activity("ike", "second", None, "200.0")

        events = await db.get_activity("ike")
        assert events[0]["event"] == "second"
        assert events[1]["event"] == "first"

    async def test_get_filters_by_session(self, db):
        await db.record_activity("ike", "event_a", None, "100.0")
        await db.record_activity("feynman", "event_b", None, "100.0")

        events = await db.get_activity("ike")
        assert len(events) == 1
        assert events[0]["session"] == "ike"

    async def test_get_since_filter(self, db):
        await db.record_activity("ike", "old", None, "100.0")
        await db.record_activity("ike", "new", None, "200.0")

        events = await db.get_activity("ike", since="150.0")
        assert len(events) == 1
        assert events[0]["event"] == "new"

    async def test_record_with_null_data(self, db):
        row_id = await db.record_activity("ike", "session_start", None, "100.0")
        assert row_id > 0

        events = await db.get_activity("ike")
        assert events[0]["data"] is None

    async def test_record_with_telemetry_metadata(self, db):
        row_id = await db.record_activity(
            "ike",
            "tool.started",
            '{"name":"exec_command"}',
            "1709500001.123456",
            entity="coding-agent",
            runtime="codex",
            source_kind="jsonl",
            source_ref="/tmp/codex.jsonl",
            source_event_id="event-1",
            trace_id="turn-123",
            parent_trace_id="session-456",
            model="gpt-5.4",
        )
        assert row_id > 0

        events = await db.get_activity("ike")
        assert events[0]["runtime"] == "codex"
        assert events[0]["source_ref"] == "/tmp/codex.jsonl"
        assert events[0]["trace_id"] == "turn-123"
        assert events[0]["model"] == "gpt-5.4"

    async def test_has_activity_event(self, db):
        await db.record_activity(
            "ike",
            "plan_notification_delivered",
            '{"channel":"tmux"}',
            "1709500001.123456",
            source_ref="tmux:ike:1709500001.123456:/tmp/plan.md",
        )

        assert (
            await db.has_activity_event(
                session="ike",
                event="plan_notification_delivered",
                source_ref="tmux:ike:1709500001.123456:/tmp/plan.md",
                since=1709500000.0,
            )
            is True
        )
        assert (
            await db.has_activity_event(
                session="ike",
                event="plan_notification_delivered",
                source_ref="tmux:ike:1709500001.123456:/tmp/plan.md",
                since=1709500002.0,
            )
            is False
        )

    async def test_record_batch(self, db):
        count = await db.record_activity_batch(
            [
                {
                    "session": "ike",
                    "event": "message.user",
                    "entity": "coding-agent",
                    "runtime": "claude",
                    "source_kind": "jsonl",
                    "source_ref": "/tmp/claude.jsonl",
                    "source_event_id": "event-1",
                    "trace_id": "trace-1",
                    "parent_trace_id": None,
                    "model": None,
                    "data": '{"content":"hello"}',
                    "ts": "101.0",
                    "received_at": "2026-03-13T00:00:00.000000Z",
                },
                {
                    "session": "ike",
                    "event": "message.assistant",
                    "entity": "coding-agent",
                    "runtime": "claude",
                    "source_kind": "jsonl",
                    "source_ref": "/tmp/claude.jsonl",
                    "source_event_id": "event-2",
                    "trace_id": "trace-1",
                    "parent_trace_id": None,
                    "model": "claude-opus-4-1",
                    "data": '{"content":"world"}',
                    "ts": "102.0",
                    "received_at": "2026-03-13T00:00:01.000000Z",
                },
            ]
        )
        assert count == 2

        events = await db.get_activity("ike")
        assert [event["event"] for event in events] == ["message.assistant", "message.user"]


class TestTelemetryCheckpoints:
    async def test_upsert_and_get_checkpoint(self, db):
        await db.upsert_telemetry_checkpoint(
            session="ike",
            source_ref="/tmp/claude.jsonl",
            runtime="claude",
            source_kind="jsonl",
            checkpoint={"offset": 128},
            entity="coding-agent",
            last_event_ts="1709500001.0",
        )

        checkpoint = await db.get_telemetry_checkpoint("ike", "/tmp/claude.jsonl")
        assert checkpoint is not None
        assert checkpoint["runtime"] == "claude"
        assert checkpoint["checkpoint"] == {"offset": 128}
        assert checkpoint["last_event_ts"] == "1709500001.0"

    async def test_upsert_checkpoint_overwrites_cursor(self, db):
        await db.upsert_telemetry_checkpoint(
            session="ike",
            source_ref="/tmp/codex.jsonl",
            runtime="codex",
            source_kind="jsonl",
            checkpoint={"offset": 10},
        )
        await db.upsert_telemetry_checkpoint(
            session="ike",
            source_ref="/tmp/codex.jsonl",
            runtime="codex",
            source_kind="jsonl",
            checkpoint={"offset": 42},
            last_event_ts="222.0",
        )

        checkpoint = await db.get_telemetry_checkpoint("ike", "/tmp/codex.jsonl")
        assert checkpoint is not None
        assert checkpoint["checkpoint"] == {"offset": 42}
        assert checkpoint["last_event_ts"] == "222.0"

    async def test_query_checkpoints_filters_by_runtime(self, db):
        await db.upsert_telemetry_checkpoint(
            session="ike",
            source_ref="/tmp/claude.jsonl",
            runtime="claude",
            source_kind="jsonl",
            checkpoint={"offset": 1},
        )
        await db.upsert_telemetry_checkpoint(
            session="ike",
            source_ref="/tmp/codex.jsonl",
            runtime="codex",
            source_kind="jsonl",
            checkpoint={"offset": 2},
        )

        checkpoints = await db.query_telemetry_checkpoints(runtime="codex")
        assert len(checkpoints) == 1
        assert checkpoints[0]["source_ref"] == "/tmp/codex.jsonl"


async def _create_swarm_with_worker(db: BackboneDB, *, status: str = "pending") -> tuple[str, str]:
    """Create a minimal non-collaborative swarm and optionally set worker status."""
    worker_name = "worker-1"
    swarm_id = await db.create_swarm(
        repo="agent-backbone",
        task_id="24",
        coding_agent_session="agent-backbone",
        workers=[
            {
                "name": worker_name,
                "role": "coder",
                "branch": "swarm/24/worker-1",
                "worktree_path": "/tmp/worker-1",
                "session": "swarm-24-worker-1",
            }
        ],
    )
    if status in {"started", "working", "pr_created"}:
        await db.update_swarm_worker_status(swarm_id, worker_name, status)
    elif status in {"done", "failed"}:
        await db.complete_swarm_worker(swarm_id, worker_name, status, f"{status} summary")
    return swarm_id, worker_name


def _worker_from_swarm(swarm: dict, worker_name: str) -> dict:
    """Extract one worker row from a swarm detail payload."""
    return next(worker for worker in swarm["workers"] if worker["name"] == worker_name)


class TestSwarmWorkerSessionReconciliation:
    async def test_reconcile_skips_pending_workers(self, db):
        swarm_id, worker_name = await _create_swarm_with_worker(db, status="pending")

        result = await db.reconcile_swarm_worker_sessions(set())
        swarm = await db.get_swarm(swarm_id)
        worker = _worker_from_swarm(swarm, worker_name)

        assert result == 0
        assert worker["status"] == "pending"
        assert worker["failure_reason"] is None

    async def test_reconcile_fails_started_worker_without_session(self, db):
        swarm_id, worker_name = await _create_swarm_with_worker(db, status="started")

        result = await db.reconcile_swarm_worker_sessions(set())
        swarm = await db.get_swarm(swarm_id)
        worker = _worker_from_swarm(swarm, worker_name)

        assert result == 1
        assert worker["status"] == "failed"
        assert worker["failure_reason"] == "session_lost"

    async def test_reconcile_fails_working_worker_without_session(self, db):
        swarm_id, worker_name = await _create_swarm_with_worker(db, status="working")

        result = await db.reconcile_swarm_worker_sessions(set())
        swarm = await db.get_swarm(swarm_id)
        worker = _worker_from_swarm(swarm, worker_name)

        assert result == 1
        assert worker["status"] == "failed"
        assert worker["failure_reason"] == "session_lost"

    async def test_reconcile_skips_done_workers(self, db):
        swarm_id, worker_name = await _create_swarm_with_worker(db, status="done")

        result = await db.reconcile_swarm_worker_sessions(set())
        swarm = await db.get_swarm(swarm_id)
        worker = _worker_from_swarm(swarm, worker_name)

        assert result == 0
        assert worker["status"] == "done"
        assert worker["failure_reason"] is None

    async def test_reconcile_skips_failed_workers(self, db):
        swarm_id, worker_name = await _create_swarm_with_worker(db, status="failed")

        result = await db.reconcile_swarm_worker_sessions(set())
        swarm = await db.get_swarm(swarm_id)
        worker = _worker_from_swarm(swarm, worker_name)

        assert result == 0
        assert worker["status"] == "failed"
        assert worker["failure_reason"] is None
