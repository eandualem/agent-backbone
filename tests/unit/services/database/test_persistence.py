"""Tests for agent_backbone/services/persistence — all use in-memory SQLite."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.support import queue_row


class TestDeliveryTracking:
    async def test_record_and_query(self, db):
        row_id = await db.deliveries.record(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            source="issue-dispatcher",
        )
        assert row_id > 0

        results = await db.deliveries.query(issue_number=42)
        assert len(results) == 1
        assert results[0]["target_entity"] == "ike"
        assert results[0]["outcome"] == "delivered"

    async def test_query_by_entity(self, db):
        await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="delivered"
        )
        await db.deliveries.record(
            issue_number=43, target_entity="feynman", session_name="feynman", outcome="delivered"
        )

        results = await db.deliveries.query(target_entity="ike")
        assert len(results) == 1
        assert results[0]["issue_number"] == 42

    async def test_query_by_outcome(self, db):
        await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="delivered"
        )
        await db.deliveries.record(
            issue_number=43, target_entity="feynman", session_name="feynman", outcome="offline"
        )

        results = await db.deliveries.query(outcome="offline")
        assert len(results) == 1
        assert results[0]["issue_number"] == 43

    async def test_query_limit(self, db):
        for i in range(10):
            await db.deliveries.record(
                issue_number=i, target_entity="ike", session_name="ike", outcome="delivered"
            )

        results = await db.deliveries.query(limit=3)
        assert len(results) == 3

    async def test_get_failed_deliveries(self, db):
        await db.deliveries.record(
            issue_number=1, target_entity="ike", session_name="ike", outcome="delivered"
        )
        await db.deliveries.record(
            issue_number=2, target_entity="feynman", session_name="feynman", outcome="offline"
        )
        await db.deliveries.record(
            issue_number=3, target_entity="leo", session_name="leo", outcome="delivery_failed"
        )
        await db.deliveries.record(
            issue_number=4, target_entity="ada", session_name="ada", outcome="agent_working"
        )

        failed = await db.deliveries.failed()
        assert len(failed) == 3
        outcomes = {r["outcome"] for r in failed}
        assert outcomes == {"offline", "delivery_failed", "agent_working"}

    async def test_get_failed_deliveries_includes_transient(self, db):
        """Every blocking delivery condition is retryable."""
        await db.deliveries.record(
            issue_number=1, target_entity="ike", session_name="ike", outcome="delivered"
        )
        await db.deliveries.record(
            issue_number=2, target_entity="feynman", session_name="feynman", outcome="human_typing"
        )
        await db.deliveries.record(
            issue_number=3, target_entity="leo", session_name="leo", outcome="settling"
        )
        await db.deliveries.record(
            issue_number=4, target_entity="ada", session_name="ada", outcome="agent_working"
        )
        await db.deliveries.record(
            issue_number=5,
            target_entity="brunel",
            session_name="brunel",
            outcome="waiting_for_human",
        )

        failed = await db.deliveries.failed()
        outcomes = {r["outcome"] for r in failed}
        assert outcomes == {"agent_working", "human_typing", "settling", "waiting_for_human"}

    async def test_get_failed_deliveries_excludes_superseded(self, db):
        """A failed delivery superseded by a later retried/delivered row is excluded."""
        await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="offline"
        )
        await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="retried"
        )

        failed = await db.deliveries.failed()
        assert len(failed) == 0

    async def test_get_failed_deliveries_excludes_superseded_by_delivered(self, db):
        """A failed delivery superseded by a later delivered row is excluded."""
        await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="offline"
        )
        await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="delivered"
        )

        failed = await db.deliveries.failed()
        assert len(failed) == 0

    async def test_get_failed_deliveries_returns_only_latest_retryable_attempt(self, db):
        """Historical blocked attempts must not multiply work on every retry tick."""
        await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="offline"
        )
        latest = await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="agent_working"
        )

        failed = await db.deliveries.failed()

        assert [row["id"] for row in failed] == [latest]

    async def test_get_failed_deliveries_includes_unsuperseded(self, db):
        """A failed delivery for entity A is not superseded by entity B's retried row."""
        await db.deliveries.record(
            issue_number=42, target_entity="coding-agent", session_name="ike", outcome="offline"
        )
        await db.deliveries.record(
            issue_number=42, target_entity="feynman", session_name="feynman", outcome="retried"
        )

        failed = await db.deliveries.failed()
        assert len(failed) == 1
        assert failed[0]["target_entity"] == "coding-agent"

    async def test_prune_old_deliveries(self, db):
        # Insert a record, then prune with 0-day retention
        await db.deliveries.record(
            issue_number=42, target_entity="ike", session_name="ike", outcome="delivered"
        )
        deleted = await db.deliveries.prune(retention_days=0)
        assert deleted == 1

        results = await db.deliveries.query()
        assert len(results) == 0

    async def test_claim_delivery_attempt_success(self, db):
        claim_id = await db.deliveries.claim(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            source="issue-dispatcher",
        )

        assert isinstance(claim_id, int)
        rows = await db.deliveries.query(issue_number=42, session_name="ike")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "attempting"

    async def test_claim_delivery_attempt_conflict(self, db):
        first = await db.deliveries.claim(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            source="issue-dispatcher",
        )
        second = await db.deliveries.claim(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            source="issue-dispatcher",
        )

        assert isinstance(first, int)
        assert second is None

    async def test_finalize_delivery_attempt(self, db):
        claim_id = await db.deliveries.claim(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            source="issue-dispatcher",
        )

        await db.deliveries.finalize(claim_id, "delivered")

        rows = await db.deliveries.query(issue_number=42, session_name="ike")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "delivered"

    async def test_reclaim_stale_attempts(self, db):
        claim_id = await db.deliveries.claim(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            source="issue-dispatcher",
        )

        async with db.engine.begin() as conn:
            await conn.execute(
                text("UPDATE deliveries SET created_at = :created_at WHERE id = :id"),
                {"created_at": "2000-01-01T00:00:00.000000Z", "id": claim_id},
            )

        reclaimed = await db.deliveries.reclaim_stale(max_age_minutes=5)

        assert reclaimed == 1
        assert await db.deliveries.query(issue_number=42, session_name="ike") == []


class TestAgentState:
    async def test_set_and_get(self, db):
        await db.states.set("ike", "idle")
        state = await db.states.get("ike")
        assert state is not None
        assert state["state"] == "idle"
        assert state["session_name"] == "ike"

    async def test_get_nonexistent(self, db):
        state = await db.states.get("nobody")
        assert state is None

    async def test_upsert(self, db):
        await db.states.set("ike", "idle")
        await db.states.set("ike", "processing_issue", current_issue=42)

        state = await db.states.get("ike")
        assert state["state"] == "processing_issue"
        assert state["current_issue"] == 42

    async def test_get_all(self, db):
        await db.states.set("ike", "idle")
        await db.states.set("feynman", "busy")

        states = await db.states.all()
        assert len(states) == 2
        names = {s["session_name"] for s in states}
        assert names == {"feynman", "ike"}

    async def test_started_at_and_ts_coalesce_on_upsert(self, db):
        await db.states.set("ike", "idle", ts="100.0", started_at="50.0")
        await db.states.set("ike", "busy", current_issue=7, reason=None)

        state = await db.states.get("ike")
        assert state["state"] == "busy"
        assert state["current_issue"] == 7
        assert state["ts"] == "100.0"
        assert state["started_at"] == "50.0"

    async def test_reason_and_repo_are_replaced_not_coalesced(self, db):
        await db.states.set(
            "ike", "waiting_for_human", reason="plan", current_repo="acme/app", plan_file="/p.md"
        )
        await db.states.set("ike", "idle")

        state = await db.states.get("ike")
        assert state["reason"] is None and state["current_repo"] is None
        assert state["plan_file"] == "/p.md"


class TestAcknowledgments:
    async def test_record_and_check_acknowledgment(self, db):
        await db.acks.record(42, "ike")
        assert await db.acks.exists(42, "ike") is True

    async def test_clear_acknowledgment(self, db):
        await db.acks.record(42, "ike")
        await db.acks.clear(42, "ike")
        assert await db.acks.exists(42, "ike") is False

    async def test_unacknowledged_by_default(self, db):
        assert await db.acks.exists(99, "nobody") is False


class TestMessageQueue:
    async def test_enqueue_and_dequeue(self, db):
        row_id = await db.queue.enqueue(
            session_name="ike",
            message="Test message",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
            source="test-flow",
        )
        assert row_id.status == "inserted" and row_id.id > 0

        messages = await db.queue.dequeue("ike")
        assert len(messages) == 1
        assert messages[0]["message"] == "Test message"
        assert messages[0]["issue_number"] == 42
        assert messages[0]["delivery_kind"] == "comment"
        assert messages[0]["status"] == "in_progress"
        assert messages[0]["leased_at"] is not None

    async def test_dequeue_empty(self, db):
        messages = await db.queue.dequeue("nobody")
        assert messages == []

    async def test_mark_delivered(self, db):
        row_id = (await db.queue.enqueue(session_name="ike", message="msg")).id
        await db.queue.dequeue("ike")
        await db.queue.mark_delivered(row_id)

        messages = await db.queue.dequeue("ike")
        assert messages == []  # delivered messages not returned

    async def test_dequeue_respects_limit(self, db):
        for i in range(5):
            await db.queue.enqueue(session_name="ike", message=f"msg-{i}")

        messages = await db.queue.dequeue("ike", limit=2)
        assert len(messages) == 2

    async def test_dequeue_oldest_first(self, db):
        await db.queue.enqueue(session_name="ike", message="first")
        await db.queue.enqueue(session_name="ike", message="second")

        messages = await db.queue.dequeue("ike")
        assert messages[0]["message"] == "first"
        assert messages[1]["message"] == "second"

    async def test_dequeue_only_own_session(self, db):
        await db.queue.enqueue(session_name="ike", message="for ike")
        await db.queue.enqueue(session_name="feynman", message="for feynman")

        messages = await db.queue.dequeue("ike")
        assert len(messages) == 1
        assert messages[0]["session_name"] == "ike"

    async def test_enqueue_without_optional_fields(self, db):
        row_id = await db.queue.enqueue(session_name="ike", message="bare message")
        assert row_id.status == "inserted" and row_id.id > 0

        messages = await db.queue.dequeue("ike")
        assert len(messages) == 1
        assert messages[0]["delivery_kind"] == "issue"
        assert messages[0]["issue_number"] is None
        assert messages[0]["target_entity"] is None

    async def test_mark_delivered_sets_timestamp(self, db):
        row_id = (await db.queue.enqueue(session_name="ike", message="msg")).id
        await db.queue.dequeue("ike")
        await db.queue.mark_delivered(row_id)

        # Use BackboneDB method to verify
        row = await queue_row(db, row_id)
        assert row["status"] == "delivered"
        assert row["delivered_at"] is not None

    async def test_multiple_sessions_independent(self, db):
        await db.queue.enqueue(session_name="ike", message="msg1", issue_number=1)
        await db.queue.enqueue(session_name="feynman", message="msg2", issue_number=2)
        await db.queue.enqueue(session_name="ike", message="msg3", issue_number=3)

        ike_msgs = await db.queue.dequeue("ike")
        feynman_msgs = await db.queue.dequeue("feynman")
        assert len(ike_msgs) == 2
        assert len(feynman_msgs) == 1

    async def test_enqueue_dedup_issue_constraint(self, db):
        first = await db.queue.enqueue(
            session_name="ike", message="first", issue_number=42, target_entity="ike"
        )
        second = await db.queue.enqueue(
            session_name="ike", message="second", issue_number=42, target_entity="ike"
        )

        assert first.status == "inserted"
        assert second.status == "already_queued" and second.id is None

        messages = await db.queue.dequeue("ike")
        assert len(messages) == 1
        assert messages[0]["message"] == "first"

    async def test_enqueue_dedup_different_issues(self, db):
        first = await db.queue.enqueue(
            session_name="ike", message="first", issue_number=42, target_entity="ike"
        )
        second = await db.queue.enqueue(
            session_name="ike", message="second", issue_number=43, target_entity="ike"
        )

        assert first.status == "inserted"
        assert second.status == "inserted"

        messages = await db.queue.dequeue("ike")
        assert len(messages) == 2
        assert {message["issue_number"] for message in messages} == {42, 43}

    async def test_enqueue_dedup_comment_constraint(self, db):
        first = await db.queue.enqueue(
            session_name="ike",
            message="same comment",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
        )
        duplicate = await db.queue.enqueue(
            session_name="ike",
            message="same comment",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
        )
        different = await db.queue.enqueue(
            session_name="ike",
            message="different comment",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
        )

        assert first.status == "inserted"
        assert duplicate.status == "already_queued"
        assert different.status == "inserted"

        messages = await db.queue.dequeue("ike")
        assert len(messages) == 2
        assert {message["message"] for message in messages} == {"same comment", "different comment"}

    async def test_enqueue_dedup_covers_every_non_issue_kind(self, db):
        # A blocked drain re-offers a queued notice through safe_deliver;
        # the queue must not grow a copy per attempt (seen live with PR notices).
        for kind in ("pull_request", "watch", "escalation"):
            first = await db.queue.enqueue(
                session_name="ike", message=f"notice {kind}", delivery_kind=kind
            )
            assert first.status == "inserted"
            again = await db.queue.enqueue(
                session_name="ike", message=f"notice {kind}", delivery_kind=kind
            )
            assert again.status == "already_queued"

    async def test_enqueue_dedup_dm_constraint(self, db):
        first = await db.queue.enqueue(
            session_name="ike",
            message="same direct message",
            delivery_kind="direct_message",
        )
        duplicate = await db.queue.enqueue(
            session_name="ike",
            message="same direct message",
            delivery_kind="direct_message",
        )
        different = await db.queue.enqueue(
            session_name="ike",
            message="different direct message",
            delivery_kind="direct_message",
        )

        assert first.status == "inserted"
        assert duplicate.status == "already_queued"
        assert different.status == "inserted"

        messages = await db.queue.dequeue("ike")
        assert len(messages) == 2
        assert {message["message"] for message in messages} == {
            "different direct message",
            "same direct message",
        }

    async def test_dedup_key_is_sender_plus_text_or_the_source_event(self, db):
        from agent_backbone.services.database._queue_repo import dedup_key_for

        row = await db.queue.enqueue(
            session_name="ike", message="hash me", delivery_kind="direct_message", sender="leo"
        )
        stored = await queue_row(db, row.id)
        assert stored["sender"] == "leo"
        assert stored["dedup_key"] == dedup_key_for("hash me", "leo", None)
        assert stored["dedup_key"].startswith("msg:")
        assert dedup_key_for("x", "leo", "comment:acme/app#1:99") == "src:comment:acme/app#1:99"

    async def test_same_text_from_two_senders_is_two_messages(self, db):
        a = await db.queue.enqueue(
            session_name="ike",
            message="run the tests",
            delivery_kind="direct_message",
            sender="leo",
        )
        b = await db.queue.enqueue(
            session_name="ike",
            message="run the tests",
            delivery_kind="direct_message",
            sender="orch",
        )
        again = await db.queue.enqueue(
            session_name="ike",
            message="run the tests",
            delivery_kind="direct_message",
            sender="leo",
        )
        assert a.status == b.status == "inserted"
        assert again.status == "already_queued"
        assert len(await db.queue.dequeue("ike")) == 2

    async def test_source_event_identity_beats_text(self, db):
        # Two GitHub comments with identical text are two messages; the same
        # comment re-offered by a blocked drain is one.
        first = await db.queue.enqueue(
            session_name="ike",
            message="LGTM",
            delivery_kind="comment",
            issue_number=7,
            target_entity="ike",
            source_key="comment:acme/app#7:100",
        )
        other = await db.queue.enqueue(
            session_name="ike",
            message="LGTM",
            delivery_kind="comment",
            issue_number=7,
            target_entity="ike",
            source_key="comment:acme/app#7:101",
        )
        again = await db.queue.enqueue(
            session_name="ike",
            message="LGTM",
            delivery_kind="comment",
            issue_number=7,
            target_entity="ike",
            source_key="comment:acme/app#7:100",
        )
        assert first.status == other.status == "inserted"
        assert again.status == "already_queued"

    async def test_get_sessions_with_pending(self, db):
        await db.queue.enqueue(
            session_name="ike", message="pending one", issue_number=42, target_entity="ike"
        )
        await db.queue.enqueue(
            session_name="jarvis", message="pending two", delivery_kind="direct_message"
        )

        sessions = await db.queue.sessions_with_pending()

        assert set(sessions) == {"ike", "jarvis"}

    async def test_dequeue_marks_in_progress(self, db):
        row_id = (
            await db.queue.enqueue(
                session_name="ike", message="claim me", issue_number=42, target_entity="ike"
            )
        ).id

        messages = await db.queue.dequeue("ike")

        assert len(messages) == 1
        assert messages[0]["id"] == row_id
        assert messages[0]["status"] == "in_progress"
        assert messages[0]["leased_at"] is not None
        row = await queue_row(db, row_id)
        assert row["status"] == "in_progress"
        assert row["leased_at"] is not None

    async def test_dequeue_skips_in_progress(self, db):
        await db.queue.enqueue(
            session_name="ike", message="claim me once", issue_number=42, target_entity="ike"
        )

        first = await db.queue.dequeue("ike")
        second = await db.queue.dequeue("ike")

        assert len(first) == 1
        assert second == []

    async def test_release_lease(self, db):
        row_id = (
            await db.queue.enqueue(
                session_name="ike", message="lease me", issue_number=42, target_entity="ike"
            )
        ).id
        await db.queue.dequeue("ike")

        await db.queue.release(row_id)

        row = await queue_row(db, row_id)
        assert row["status"] == "pending"
        assert row["leased_at"] is None

    async def test_expire_stale_leases(self, db):
        row_id = (
            await db.queue.enqueue(
                session_name="ike", message="stale lease", issue_number=42, target_entity="ike"
            )
        ).id
        await db.queue.dequeue("ike")

        async with db.engine.begin() as conn:
            await conn.execute(
                text("UPDATE message_queue SET leased_at = :leased_at WHERE id = :id"),
                {"leased_at": "2000-01-01T00:00:00.000000Z", "id": row_id},
            )

        expired = await db.queue.expire_stale_leases(max_age_minutes=5)

        assert expired == 1
        row = await queue_row(db, row_id)
        assert row["status"] == "pending"
        assert row["leased_at"] is None

    async def test_purge_covers_in_progress(self, db):
        first = (
            await db.queue.enqueue(
                session_name="ike", message="claim me", issue_number=775, target_entity="ike"
            )
        ).id
        second = (
            await db.queue.enqueue(
                session_name="feynman",
                message="leave pending",
                issue_number=775,
                target_entity="feynman",
            )
        ).id
        await db.queue.dequeue("ike")

        purged = await db.queue.purge_for_issue(775)

        assert purged == 2
        assert (await queue_row(db, first))["status"] == "delivered"
        assert (await queue_row(db, second))["status"] == "delivered"

    async def test_mark_delivered_requires_in_progress(self, db):
        row_id = (
            await db.queue.enqueue(
                session_name="ike", message="pending row", issue_number=42, target_entity="ike"
            )
        ).id

        await db.queue.mark_delivered(row_id)

        row = await queue_row(db, row_id)
        assert row["status"] == "pending"
        assert row["delivered_at"] is None


class TestSwarmNameConflict:
    async def test_second_active_create_raises(self, db):
        fields = dict(
            repo="acme/app",
            issue_number=1,
            initiator="",
            coordinator="s-c",
            branch="b",
            worktree_dir="/w",
        )
        await db.swarms.create("s", **fields)
        with pytest.raises(ValueError, match="already active"):
            await db.swarms.create("s", **{**fields, "issue_number": 2})
        await db.swarms.set_status("s", "done")
        await db.swarms.create("s", **{**fields, "issue_number": 3})  # a finished name is reusable
        assert (await db.swarms.get("s"))["issue_number"] == 3


class TestAgentAlwaysOn:
    async def test_always_on_round_trips_and_defaults_off(self, db):
        from agent_backbone.config import agents_from_rows

        common = {
            "dir": "/tmp/a",
            "runtime": "claude",
            "model": None,
            "repo": "",
            "tags": [],
            "env": {},
        }
        await db.agents.upsert("quiet", description="", **common)
        await db.agents.upsert("vital", description="", always_on=True, **common)
        rows = await db.agents.list()
        by_name = {row["name"]: row for row in rows}
        assert by_name["quiet"]["always_on"] is False
        assert by_name["vital"]["always_on"] is True
        agents = agents_from_rows(rows)
        assert agents.get("vital").always_on and not agents.get("quiet").always_on

    async def test_unattended_round_trips_and_defaults_off(self, db):
        from agent_backbone.config import agents_from_rows

        common = {
            "dir": "/tmp/a",
            "runtime": "codex",
            "model": None,
            "repo": "",
            "tags": [],
            "env": {},
            "description": "",
        }
        await db.agents.upsert("asks", **common)
        await db.agents.upsert("free", unattended=True, **common)
        rows = await db.agents.list()
        by_name = {row["name"]: row for row in rows}
        assert by_name["asks"]["unattended"] is False
        assert by_name["free"]["unattended"] is True
        agents = agents_from_rows(rows)
        assert agents.get("free").unattended and not agents.get("asks").unattended
