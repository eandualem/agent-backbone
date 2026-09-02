"""Tests for jobs/retry.py — retry, queue drain, and dedup semantics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.services.jobs.retry import delivery_retry, drain_message_queue, retry_delivery
from tests.conftest import TEST_REPO, make_config
from tests.support import queue_row


class TestRetryDeliveryAckCheck:
    async def test_retry_skips_acknowledged_target_entity(self, db, config):
        await db.record_delivery(
            issue_number=154,
            target_entity="feynman",
            session_name="ike",
            outcome="offline",
            repo=TEST_REPO,
        )
        await db.record_acknowledgment(154, "feynman", repo=TEST_REPO)
        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "feynman",
            "repo": TEST_REPO,
        }

        assert await retry_delivery(config, delivery, db, AsyncMock()) == "acknowledged"

    async def test_retry_skips_when_session_acknowledged(self, db, config):
        await db.record_delivery(
            issue_number=154,
            target_entity="feynman",
            session_name="ike",
            outcome="offline",
            repo=TEST_REPO,
        )
        await db.record_acknowledgment(154, "ike", repo=TEST_REPO)
        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "feynman",
            "repo": TEST_REPO,
        }

        assert await retry_delivery(config, delivery, db, AsyncMock()) == "acknowledged"

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_retry_proceeds_when_not_acknowledged(self, mock_deliver, db, config):
        await db.record_delivery(
            issue_number=154,
            target_entity="ike",
            session_name="ike",
            outcome="offline",
            repo=TEST_REPO,
        )
        mock_issue = MagicMock(state="open", repo_full_name=TEST_REPO)
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
        await db.record_delivery(
            issue_number=77,
            target_entity="backbone",
            session_name="backbone",
            outcome="offline",
            repo="acme/backbone",
        )
        mock_issue = MagicMock(state="open", repo_full_name="acme/backbone")
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(return_value=mock_issue)
        mock_gh.list_issues = AsyncMock(return_value=[MagicMock(number=77)])
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
        mock_gh.get_issue = AsyncMock(return_value=MagicMock(state="open", repo_full_name=""))
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
        await db.enqueue_message(
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
        await db.enqueue_message(
            session_name="ike",
            message="Direct payload",
            delivery_kind="direct_message",
            source="api-messages",
        )
        db.release_lease = AsyncMock(wraps=db.release_lease)
        mock_deliver.return_value = "offline"

        summary = await drain_message_queue(config, db, AsyncMock(), active_sessions={"ike"})

        assert summary == {}
        db.release_lease.assert_awaited_once_with(1)
        row = await queue_row(db, 1)
        assert row["status"] == "pending"
        assert row["leased_at"] is None

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    async def test_drain_releases_every_leased_row_on_block(self, mock_deliver, db, config):
        """A blocked head must not strand the rest of the batch in_progress."""
        for i in range(3):
            await db.enqueue_message(
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

    async def test_drain_calls_expire_stale_leases(self, db, config):
        db.expire_stale_leases = AsyncMock(return_value=0)
        db.expire_stale_pending = AsyncMock(return_value=0)

        await drain_message_queue(config, db, AsyncMock(), active_sessions=set())

        db.expire_stale_leases.assert_awaited_once_with(max_age_minutes=5)
        db.expire_stale_pending.assert_awaited_once_with(max_age_minutes=30)

    @patch("agent_backbone.services.jobs.retry.safe_deliver", new_callable=AsyncMock)
    @patch(
        "agent_backbone.services.jobs.retry.list_open_queue_for_target",
        new_callable=AsyncMock,
    )
    @patch("agent_backbone.services.terminal.list_sessions", new_callable=AsyncMock)
    async def test_queue_drain_runs_without_failed_issue_rows(
        self, mock_list_sessions, mock_queue, mock_deliver, db, config
    ):
        await db.enqueue_message(
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
    @patch("agent_backbone.services.terminal.list_sessions", new_callable=AsyncMock)
    async def test_queue_drain_delivers_direct_messages_without_issue_metadata(
        self, mock_list_sessions, mock_queue, mock_deliver, db, config
    ):
        await db.enqueue_message(
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
    @patch("agent_backbone.services.terminal.list_sessions", new_callable=AsyncMock)
    async def test_delivery_retry_without_github_only_drains_queue(
        self, mock_list_sessions, mock_deliver, db, config
    ):
        """No GitHub client → failed issue rows are left alone but the queue still drains."""
        await db.record_delivery(
            issue_number=5,
            target_entity="ike",
            session_name="ike",
            outcome="offline",
            repo=TEST_REPO,
        )
        await db.enqueue_message(session_name="ike", message="hi", delivery_kind="direct_message")
        mock_list_sessions.return_value = ["ike"]
        mock_deliver.return_value = "delivered"

        summary = await delivery_retry(config, db, None)

        assert summary == {"queue_delivered": 1}
        assert mock_deliver.await_count == 1


class TestPurgePendingForIssue:
    async def test_purges_pending_messages_for_issue(self, db):
        for session, number in (("feynman", 775), ("ike", 775), ("feynman", 776)):
            await db.enqueue_message(
                session_name=session,
                message=f"Comment {number}",
                issue_number=number,
                target_entity=session,
                delivery_kind="comment",
            )

        assert await db.purge_pending_for_issue(775) == 2
        assert (await queue_row(db, 1))["status"] == "delivered"
        assert (await queue_row(db, 2))["status"] == "delivered"
        assert (await queue_row(db, 3))["status"] == "pending"

    async def test_purge_returns_zero_when_no_pending(self, db):
        assert await db.purge_pending_for_issue(999) == 0


class TestDeliveryDedupPrefixedOutcomes:
    async def test_comment_failures_are_not_retried_as_issues(self, db):
        await db.record_delivery(
            issue_number=100,
            target_entity="feynman",
            session_name="feynman",
            outcome="offline",
            kind="comment",
        )
        assert 100 not in [r["issue_number"] for r in await db.get_failed_deliveries()]

    async def test_unprefixed_delivered_still_suppresses(self, db):
        await db.record_delivery(
            issue_number=101, target_entity="ike", session_name="ike", outcome="offline"
        )
        await db.record_delivery(
            issue_number=101, target_entity="ike", session_name="ike", outcome="delivered"
        )
        assert 101 not in [r["issue_number"] for r in await db.get_failed_deliveries()]

    async def test_unsuppressed_failure_still_retried(self, db):
        await db.record_delivery(
            issue_number=102, target_entity="ike", session_name="ike", outcome="offline"
        )
        assert 102 in [r["issue_number"] for r in await db.get_failed_deliveries()]

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

        await db.record_delivery(
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
        assert 300 not in [r["issue_number"] for r in await db.get_failed_deliveries()]


class TestOutcomeQueues:
    def test_direct_message_queues_on_every_block(self):
        from agent_backbone.services.routing._delivery import outcome_queues

        blocked = ("offline", "waiting_for_human", "agent_working", "human_typing", "settling")
        for outcome in blocked:
            assert outcome_queues(outcome, "direct_message") is True
        assert outcome_queues("delivery_failed", "direct_message") is True
        assert outcome_queues("delivered", "direct_message") is False

    def test_issue_kind_queues_only_offline(self):
        from agent_backbone.services.routing._delivery import outcome_queues

        assert outcome_queues("offline", "issue") is True
        assert outcome_queues("agent_working", "issue") is False
        assert outcome_queues("already_delivered", "issue") is False
