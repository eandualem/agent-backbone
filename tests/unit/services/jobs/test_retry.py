"""Tests for jobs/retry.py — retry, queue drain, and dedup semantics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.services.jobs.retry import delivery_retry, drain_message_queue, retry_delivery
from tests.conftest import TEST_REPO, make_config
from tests.support import queue_row


class TestRetryDeliveryAckCheck:
    async def test_retry_skips_acknowledged_target_entity(self, db, config):
        await db.deliveries.record(
            issue_number=154,
            target_entity="feynman",
            session_name="ike",
            outcome="offline",
            repo=TEST_REPO,
        )
        await db.acks.record(154, "feynman", repo=TEST_REPO)
        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "feynman",
            "repo": TEST_REPO,
        }

        assert await retry_delivery(config, delivery, db, AsyncMock()) == "acknowledged"

    async def test_retry_skips_when_session_acknowledged(self, db, config):
        await db.deliveries.record(
            issue_number=154,
            target_entity="feynman",
            session_name="ike",
            outcome="offline",
            repo=TEST_REPO,
        )
        await db.acks.record(154, "ike", repo=TEST_REPO)
        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "feynman",
            "repo": TEST_REPO,
        }

        assert await retry_delivery(config, delivery, db, AsyncMock()) == "acknowledged"

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_retry_proceeds_when_not_acknowledged(self, mock_deliver, db, config):
        await db.deliveries.record(
            issue_number=154,
            target_entity="ike",
            session_name="ike",
            outcome="offline",
            repo=TEST_REPO,
        )
        mock_issue = IssueData(
            number=154, title="Work", repo_full_name=TEST_REPO, labels=ParsedLabels(targets=["ike"])
        )
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(return_value=mock_issue)
        mock_deliver.return_value = "delivered"
        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "ike",
            "repo": TEST_REPO,
        }

        assert await retry_delivery(config, delivery, db, mock_gh) == "retried"
        mock_deliver.assert_called_once()

    async def test_retry_issue_closed(self, db, config):
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(return_value=MagicMock(state="closed"))
        delivery = {
            "session_name": "ike",
            "issue_number": 1,
            "target_entity": "ike",
            "repo": TEST_REPO,
        }

        assert await retry_delivery(config, delivery, db, mock_gh) == "issue_closed"

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_retry_repo_owner_issue_fetches_from_owned_repo(self, mock_deliver, db, tmp_path):
        """An agent that owns a repo has its issues fetched from that repo."""
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        await db.deliveries.record(
            issue_number=77,
            target_entity="backbone",
            session_name="backbone",
            outcome="offline",
            repo="acme/backbone",
        )
        mock_issue = IssueData(number=77, title="Work", repo_full_name="acme/backbone")
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(return_value=mock_issue)
        mock_gh.list_issues = AsyncMock(return_value=[mock_issue])
        mock_deliver.return_value = "delivered"
        delivery = {
            "session_name": "backbone",
            "issue_number": 77,
            "target_entity": "backbone",
            "repo": "acme/backbone",
        }

        assert await retry_delivery(config, delivery, db, mock_gh) == "retried"
        assert mock_gh.get_issue.await_args.kwargs["repo_full_name"] == "acme/backbone"

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_retry_maps_busy_outcomes(self, mock_deliver, db, config):
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(
            return_value=IssueData(
                number=88,
                title="Work",
                repo_full_name=TEST_REPO,
                labels=ParsedLabels(targets=["ike"]),
            )
        )
        delivery = {
            "session_name": "ike",
            "issue_number": 88,
            "target_entity": "ike",
            "repo": TEST_REPO,
        }

        mock_deliver.return_value = "agent_working"
        assert await retry_delivery(config, delivery, db, mock_gh) == "still_busy"
        mock_deliver.return_value = "offline"
        assert await retry_delivery(config, delivery, db, mock_gh) == "still_offline"


class TestDeliveryRetryQueueDrain:
    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_drain_includes_queued_sessions(self, mock_deliver, db, config):
        await db.queue.enqueue(
            session_name="scratch",
            message="Queued",
            delivery_kind="direct_message",
            source="api-messages",
        )
        mock_deliver.return_value = "delivered"

        summary = await drain_message_queue(config, db, AsyncMock(), active_sessions=set())

        assert summary["queue_delivered"] == 1
        assert mock_deliver.await_args.args[0] == "scratch"
        assert (await queue_row(db, 1))["status"] == "delivered"

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_drain_releases_lease_on_failure(self, mock_deliver, db, config):
        await db.queue.enqueue(
            session_name="ike",
            message="Direct payload",
            delivery_kind="direct_message",
            source="api-messages",
        )
        db.queue.release = AsyncMock(wraps=db.queue.release)
        mock_deliver.return_value = "offline"

        summary = await drain_message_queue(config, db, AsyncMock(), active_sessions={"ike"})

        assert summary == {}
        db.queue.release.assert_awaited_once_with(1)
        row = await queue_row(db, 1)
        assert row["status"] == "pending"
        assert row["leased_at"] is None

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_drain_releases_every_leased_row_on_block(self, mock_deliver, db, config):
        """A blocked head must not strand the rest of the batch in_progress."""
        for i in range(3):
            await db.queue.enqueue(
                session_name="ike",
                message=f"payload {i}",
                delivery_kind="direct_message",
                source="api-messages",
            )
        mock_deliver.return_value = "agent_working"

        await drain_message_queue(config, db, AsyncMock(), active_sessions={"ike"})

        mock_deliver.assert_awaited_once()  # stops at the head, order preserved
        for message_id in (1, 2, 3):
            row = await queue_row(db, message_id)
            assert row["status"] == "pending"
            assert row["leased_at"] is None

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    @patch(
        "agent_backbone.services.jobs.retry.list_open_queue_for_target",
        new_callable=AsyncMock,
        side_effect=RuntimeError("GitHub 502"),
    )
    @patch("agent_backbone.services.jobs.retry.list_sessions", new_callable=AsyncMock)
    async def test_a_scope_lookup_failure_defers_instead_of_widening_the_gate(
        self, mock_list_sessions, mock_scope, mock_deliver, db, config
    ):
        await db.queue.enqueue(
            session_name="ike",
            message="Issue payload",
            issue_number=91,
            target_entity="ike",
            delivery_kind="issue",
            repo=TEST_REPO,
        )
        mock_list_sessions.return_value = ["ike"]
        summary = await drain_message_queue(
            config=config, db=db, gh=MagicMock(), active_sessions={"ike"}
        )
        mock_deliver.assert_not_called()
        assert summary.get("queue_deferred") == 1
        # the row went back to pending for the next drain
        assert await db.queue.pending_count("ike") == 1

    async def test_expired_messages_leave_a_delivery_record_in_the_same_transaction(
        self, db, config
    ):
        from sqlalchemy import text

        await db.queue.enqueue(
            session_name="ike",
            message="[via:backbone from:leo] are you there?",
            delivery_kind="direct_message",
            source="backbone",
        )
        async with db.queue._tx() as conn:  # age the row past the expiry
            await conn.execute(
                text("UPDATE message_queue SET enqueued_at = '2000-01-01T00:00:00+00:00'")
            )
        expired = await db.queue.expire_pending(max_age_minutes=30)
        assert len(expired) == 1 and expired[0]["session_name"] == "ike"
        rows = await db.deliveries.query(session_name="ike", limit=5, kind="direct_message")
        assert rows and rows[0]["outcome"] == "expired"
        assert rows[0]["preview"].startswith("[via:backbone from:leo]")
        assert await db.queue.pending_count("ike") == 0

    async def test_the_drain_counts_expiries(self, db, config):
        db.queue.expire_pending = AsyncMock(return_value=[{"id": 7, "session_name": "ike"}])
        db.queue.expire_stale_leases = AsyncMock(return_value=0)
        db.queue.sessions_with_pending = AsyncMock(return_value=[])
        with patch("agent_backbone.services.jobs.retry.list_sessions", new_callable=AsyncMock):
            summary = await drain_message_queue(
                config=config, db=db, gh=None, active_sessions=set()
            )
        assert summary["queue_expired"] == 1

    async def test_drain_calls_expire_stale_leases(self, db, config):
        db.queue.expire_stale_leases = AsyncMock(return_value=0)
        db.queue.expire_pending = AsyncMock(return_value=0)

        await drain_message_queue(config, db, AsyncMock(), active_sessions=set())

        db.queue.expire_stale_leases.assert_awaited_once_with(max_age_minutes=5)
        db.queue.expire_pending.assert_awaited_once_with(max_age_minutes=30)

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    @patch(
        "agent_backbone.services.jobs.retry.list_open_queue_for_target",
        new_callable=AsyncMock,
    )
    @patch("agent_backbone.services.jobs.retry.list_sessions", new_callable=AsyncMock)
    async def test_queue_drain_runs_without_failed_issue_rows(
        self, mock_list_sessions, mock_queue, mock_deliver, db, config
    ):
        await db.queue.enqueue(
            session_name="ike",
            message="Comment payload",
            issue_number=91,
            target_entity="ike",
            delivery_kind="comment",
            source="issue-dispatcher",
        )
        mock_list_sessions.return_value = ["ike"]
        mock_queue.return_value = [MagicMock(number=91)]
        mock_deliver.return_value = "delivered"

        summary = await delivery_retry(config, db, AsyncMock())

        assert summary["queue_delivered"] == 1
        assert mock_deliver.await_args.kwargs["delivery_kind"] == "comment"
        assert (await queue_row(db, 1))["status"] == "delivered"

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    @patch(
        "agent_backbone.services.jobs.retry.list_open_queue_for_target",
        new_callable=AsyncMock,
    )
    @patch("agent_backbone.services.jobs.retry.list_sessions", new_callable=AsyncMock)
    async def test_queue_drain_delivers_direct_messages_without_issue_metadata(
        self, mock_list_sessions, mock_queue, mock_deliver, db, config
    ):
        await db.queue.enqueue(
            session_name="ike",
            message="Direct payload",
            delivery_kind="direct_message",
            source="api-messages",
        )
        mock_list_sessions.return_value = ["ike"]
        mock_deliver.return_value = "delivered"

        summary = await delivery_retry(config, db, AsyncMock())

        assert summary["queue_delivered"] == 1
        assert mock_deliver.await_args.kwargs["issue_number"] is None
        assert mock_deliver.await_args.kwargs["target_entity"] is None
        assert mock_deliver.await_args.kwargs["delivery_kind"] == "direct_message"
        mock_queue.assert_not_called()

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    @patch("agent_backbone.services.jobs.retry.list_sessions", new_callable=AsyncMock)
    async def test_delivery_retry_without_github_only_drains_queue(
        self, mock_list_sessions, mock_deliver, db, config
    ):
        """No GitHub client → failed issue rows are left alone but the queue still drains."""
        await db.deliveries.record(
            issue_number=5,
            target_entity="ike",
            session_name="ike",
            outcome="offline",
            repo=TEST_REPO,
        )
        await db.queue.enqueue(session_name="ike", message="hi", delivery_kind="direct_message")
        mock_list_sessions.return_value = ["ike"]
        mock_deliver.return_value = "delivered"

        summary = await delivery_retry(config, db, None)

        assert summary == {"queue_delivered": 1}
        assert mock_deliver.await_count == 1


class TestDrainKeepsTheRowIdentity:
    async def test_aged_blocked_direct_message_keeps_one_row_then_delivers_once(self, config, db):
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import text

        from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

        original = "[via:backbone from:alice] status?"
        await db.queue.enqueue(
            session_name="ike", message=original, delivery_kind="direct_message", sender="alice"
        )
        profile = SessionProfile(session_name="ike", intelligence=SessionIntelligence.AGENT_WORKING)
        with patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            AsyncMock(return_value=profile),
        ):
            for minutes in (3, 4, 5):
                async with db.engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE message_queue SET enqueued_at = :t"),
                        {"t": (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()},
                    )
                await drain_message_queue(config, db, None, active_sessions={"ike"})
                assert await db.queue.pending_count("ike") == 1
                assert (await queue_row(db, 1))["message"] == original
        ready = SessionProfile(session_name="ike", intelligence=SessionIntelligence.READY)
        with (
            patch(
                "agent_backbone.services.routing._delivery.get_session_intelligence",
                AsyncMock(return_value=ready),
            ),
            patch(
                "agent_backbone.services.routing._delivery.send_message",
                AsyncMock(return_value=True),
            ) as send,
        ):
            await drain_message_queue(config, db, None, active_sessions={"ike"})
            await drain_message_queue(config, db, None, active_sessions={"ike"})
        send.assert_awaited_once()
        assert "(queued 5 min ago)" in send.await_args.args[1]

    async def test_concurrent_drains_do_not_pass_a_blocked_batch(self, config, db):
        import asyncio

        entered, release = asyncio.Event(), asyncio.Event()
        for number in range(6):
            await db.queue.enqueue(
                session_name="ike", message=str(number), delivery_kind="direct_message"
            )

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return "agent_working"

        with patch(
            "agent_backbone.services.jobs.retry.safe_deliver", AsyncMock(side_effect=blocked)
        ) as send:
            first = asyncio.create_task(
                drain_message_queue(config, db, None, active_sessions={"ike"})
            )
            await entered.wait()
            try:
                await asyncio.wait_for(
                    drain_message_queue(config, db, None, active_sessions={"ike"}), timeout=1
                )
                send.assert_awaited_once()
            finally:
                release.set()
                await first
        assert await db.queue.pending_count("ike") == 6

    async def test_cancelled_drain_releases_its_batch(self, config, db):
        import asyncio

        entered = asyncio.Event()
        await db.queue.enqueue(session_name="ike", message="first", delivery_kind="direct_message")

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch(
            "agent_backbone.services.jobs.retry.safe_deliver", AsyncMock(side_effect=blocked)
        ):
            task = asyncio.create_task(
                drain_message_queue(config, db, None, active_sessions={"ike"})
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await db.queue.pending_count("ike") == 1
        with patch(
            "agent_backbone.services.jobs.retry.safe_deliver", AsyncMock(return_value="delivered")
        ):
            assert (await drain_message_queue(config, db, None, active_sessions={"ike"}))[
                "queue_delivered"
            ] == 1

    @pytest.mark.parametrize(
        "state, targets, sender",
        [
            ("open", ["feynman"], "leo"),
            ("closed", ["ike"], "leo"),
            ("open", ["ike"], "ike"),
        ],
    )
    async def test_stale_issue_is_cleared_on_retry_and_drain(
        self, config, db, state, targets, sender
    ):
        await db.queue.enqueue(
            session_name="ike",
            message="Old work",
            repo=TEST_REPO,
            issue_number=7,
            target_entity="ike",
            delivery_kind="issue",
        )
        gh = AsyncMock()
        gh.get_issue.return_value = IssueData(
            number=7,
            title="Current work",
            state=state,
            repo_full_name=TEST_REPO,
            labels=ParsedLabels(targets=targets, sender=sender),
        )
        with patch("agent_backbone.services.jobs.retry.safe_deliver", AsyncMock()) as send:
            status = await retry_delivery(
                config,
                {
                    "session_name": "ike",
                    "target_entity": "ike",
                    "repo": TEST_REPO,
                    "issue_number": 7,
                },
                db,
                gh,
            )
            summary = await drain_message_queue(config, db, gh, active_sessions={"ike"})
        assert status == ("issue_closed" if state == "closed" else "no_longer_targeted")
        assert summary == {"queue_cleared": 1}
        send.assert_not_awaited()
        assert await db.queue.pending_count("ike") == 0

    async def test_explicit_target_outside_tracked_queue_is_still_retried(self, config, db):
        gh = AsyncMock()
        gh.get_issue.return_value = IssueData(
            number=7,
            title="External work",
            repo_full_name="elsewhere/repo",
            labels=ParsedLabels(targets=["ike"]),
        )
        gh.list_issues.return_value = []
        with patch(
            "agent_backbone.services.jobs.retry.safe_deliver", AsyncMock(return_value="delivered")
        ):
            assert (
                await retry_delivery(
                    config,
                    {
                        "session_name": "ike",
                        "target_entity": "ike",
                        "repo": "elsewhere/repo",
                        "issue_number": 7,
                    },
                    db,
                    gh,
                )
                == "retried"
            )

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    @patch("agent_backbone.services.jobs.retry.list_sessions", new_callable=AsyncMock)
    async def test_blocked_comment_keeps_its_existing_row(
        self, mock_list_sessions, mock_deliver, db, config
    ):
        """A leased comment row that is still blocked must fold back into itself —
        re-offering it under a text key would add a second row and deliver twice."""
        await db.queue.enqueue(
            session_name="ike",
            message="LGTM",
            delivery_kind="comment",
            issue_number=7,
            target_entity="ike",
            repo=TEST_REPO,
            sender="leo",
            source_key=f"comment:{TEST_REPO}#7:100",
        )
        mock_list_sessions.return_value = ["ike"]
        mock_deliver.return_value = "agent_working"

        await delivery_retry(config, db, None)

        kwargs = mock_deliver.await_args.kwargs
        assert kwargs["sender"] == "leo"
        assert kwargs["requeue"] is False
        assert await db.queue.pending_count("ike") == 1

    async def test_reoffer_of_a_leased_row_never_adds_a_second_one(self, db):
        first = await db.queue.enqueue(
            session_name="ike",
            message="LGTM",
            delivery_kind="comment",
            issue_number=7,
            target_entity="ike",
            source_key="comment:acme/app#7:100",
        )
        leased = await db.queue.dequeue("ike")  # in_progress while safe_deliver re-offers it
        assert [row["id"] for row in leased] == [first.id]
        again = await db.queue.enqueue(
            session_name="ike",
            message="LGTM",
            delivery_kind="comment",
            issue_number=7,
            target_entity="ike",
            source_key="comment:acme/app#7:100",
        )
        assert again.status == "already_queued"
        await db.queue.release(first.id)
        assert len(await db.queue.dequeue("ike")) == 1

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    @patch("agent_backbone.services.jobs.retry.list_sessions", new_callable=AsyncMock)
    async def test_the_drain_never_asks_for_its_row_to_be_stored_again(
        self, mock_list_sessions, mock_deliver, db, config
    ):
        await db.queue.enqueue(session_name="ike", message="hi", delivery_kind="direct_message")
        mock_list_sessions.return_value = ["ike"]
        mock_deliver.return_value = "agent_working"
        await delivery_retry(config, db, None)
        assert mock_deliver.await_args.kwargs["requeue"] is False

    @patch("agent_backbone.services.jobs.retry.list_sessions", new_callable=AsyncMock)
    async def test_a_blocked_direct_message_stays_one_unstamped_row(
        self, mock_list_sessions, db, config
    ):
        """Seen live: the re-offer carried a '(queued N min ago)' stamp, so it was
        stored as a *new* row under a new text key; the next drain re-offered
        both, and one more copy appeared per minute — nine rows, three
        deliveries of one message. The leased row is the stored copy."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import text

        from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

        result = await db.queue.enqueue(
            session_name="ike",
            message="[via:backbone from:leo] hello",
            delivery_kind="direct_message",
        )
        # Old enough for the offered text to carry the age stamp.
        long_ago = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        async with db.engine.begin() as conn:
            await conn.execute(
                text("UPDATE message_queue SET enqueued_at = :t WHERE id = :id"),
                {"t": long_ago, "id": result.id},
            )
        mock_list_sessions.return_value = ["ike"]
        busy = SessionProfile("ike", SessionIntelligence.AGENT_WORKING)
        with patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            new_callable=AsyncMock,
            return_value=busy,
        ):
            await delivery_retry(config, db, None)
            await delivery_retry(config, db, None)

        assert await db.queue.pending_count("ike") == 1
        row = await queue_row(db, result.id)
        assert row["status"] == "pending"
        assert row["message"] == "[via:backbone from:leo] hello"  # never stamped in storage


class TestPurgePendingForIssue:
    async def test_purges_pending_messages_for_issue(self, db):
        for session, number in (("feynman", 775), ("ike", 775), ("feynman", 776)):
            await db.queue.enqueue(
                session_name=session,
                message=f"Comment {number}",
                issue_number=number,
                target_entity=session,
                delivery_kind="comment",
            )

        assert await db.queue.purge_for_issue(775) == 2
        assert (await queue_row(db, 1))["status"] == "delivered"
        assert (await queue_row(db, 2))["status"] == "delivered"
        assert (await queue_row(db, 3))["status"] == "pending"

    async def test_purge_returns_zero_when_no_pending(self, db):
        assert await db.queue.purge_for_issue(999) == 0


class TestDeliveryDedupPrefixedOutcomes:
    async def test_comment_failures_are_not_retried_as_issues(self, db):
        await db.deliveries.record(
            issue_number=100,
            target_entity="feynman",
            session_name="feynman",
            outcome="offline",
            kind="comment",
        )
        assert 100 not in [r["issue_number"] for r in await db.deliveries.failed()]

    async def test_unprefixed_delivered_still_suppresses(self, db):
        await db.deliveries.record(
            issue_number=101, target_entity="ike", session_name="ike", outcome="offline"
        )
        await db.deliveries.record(
            issue_number=101, target_entity="ike", session_name="ike", outcome="delivered"
        )
        assert 101 not in [r["issue_number"] for r in await db.deliveries.failed()]

    async def test_unsuppressed_failure_still_retried(self, db):
        await db.deliveries.record(
            issue_number=102, target_entity="ike", session_name="ike", outcome="offline"
        )
        assert 102 in [r["issue_number"] for r in await db.deliveries.failed()]

    @patch(
        "agent_backbone.services.routing._delivery.get_session_intelligence",
        new_callable=AsyncMock,
    )
    @patch("agent_backbone.services.routing._delivery.send_message", new_callable=AsyncMock)
    async def test_comment_dedup_does_not_block_new_comments(
        self, mock_send, mock_intel, db, config
    ):
        """A prior comment_delivered must NOT block new comments on the same issue."""
        from agent_backbone.services.routing._delivery import safe_deliver
        from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

        await db.deliveries.record(
            issue_number=200,
            target_entity="feynman",
            session_name="feynman",
            outcome="delivered",
            kind="comment",
        )
        mock_intel.return_value = SessionProfile(
            session_name="feynman",
            intelligence=SessionIntelligence.READY,
            agent_state="idle",
            runtime="shell",
        )
        mock_send.return_value = True

        outcome = await safe_deliver(
            "feynman",
            "New comment on same issue",
            config,
            db=db,
            issue_number=200,
            target_entity="feynman",
            source="test",
            delivery_kind="comment",
        )

        assert outcome == "delivered"
        mock_send.assert_called_once()


class TestIssueRedeliveryRegression:
    """The old system's repeat-delivery bug: after a successful issue
    delivery, later activity on the issue (comments, relabels, poll/webhook
    overlap, the retry job) must never paste the issue notification again."""

    @patch(
        "agent_backbone.services.routing._delivery.get_session_intelligence",
        new_callable=AsyncMock,
    )
    @patch("agent_backbone.services.routing._delivery.send_message", new_callable=AsyncMock)
    async def test_delivered_issue_is_never_redelivered(self, mock_send, mock_intel, db, config):
        from agent_backbone.services.routing._delivery import safe_deliver
        from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

        mock_intel.return_value = SessionProfile(
            session_name="ike",
            intelligence=SessionIntelligence.READY,
            agent_state="idle",
            runtime="shell",
        )
        mock_send.return_value = True

        def deliver(kind: str):
            return safe_deliver(
                "ike",
                f"{kind} for issue 300",
                config,
                db=db,
                repo=TEST_REPO,
                issue_number=300,
                target_entity="ike",
                source="issue-dispatcher",
                delivery_kind=kind,
            )

        assert await deliver("issue") == "delivered"
        # A comment on the issue is its own kind and goes through …
        assert await deliver("comment") == "delivered"
        # … but every re-dispatch of the issue itself is suppressed.
        assert await deliver("issue") == "already_delivered"
        assert await deliver("issue") == "already_delivered"
        assert mock_send.await_count == 2
        # And the retry job no longer sees the issue as failed.
        assert 300 not in [r["issue_number"] for r in await db.deliveries.failed()]


class TestQueuedAge:
    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_a_long_queued_message_is_delivered_with_its_age(self, mock_deliver, db, config):
        await db.queue.enqueue(
            session_name="ike",
            message="[via:github pr:138] Review on acme/app#138",
            delivery_kind="review",
            source="github",
        )
        # Backdate the row: it waited twenty minutes while the agent was busy.
        from datetime import UTC, datetime, timedelta

        old = (datetime.now(UTC) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        async with db.queue._tx() as conn:
            from sqlalchemy import text

            await conn.execute(text("UPDATE message_queue SET enqueued_at = :t"), {"t": old})
        mock_deliver.return_value = "delivered"

        await drain_message_queue(config, db, AsyncMock(), active_sessions={"ike"})

        delivered = mock_deliver.await_args.args[1]
        assert delivered.startswith("[via:github pr:138] (queued 20 min ago) Review on")
