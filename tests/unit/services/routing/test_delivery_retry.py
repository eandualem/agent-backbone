"""Tests for flows/delivery_retry.py — ack check in retry flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.services.database import BackboneDB
from agent_backbone.services.registry import RepoInfo


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


class TestRetryDeliveryAckCheck:
    """Tests for the acknowledgment check added to retry_delivery."""

    async def test_retry_skips_acknowledged_target_entity(self, db, config):
        """Retry skips delivery when target_entity has acknowledged the issue."""
        # Record a failed delivery and an ack for the target entity
        await db.record_delivery(154, "coding-agent", "ike", "offline")
        await db.record_acknowledgment(154, "coding-agent")

        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "coding-agent",
        }

        from agent_backbone.services.routing import retry_delivery

        mock_gh = AsyncMock()
        result = await retry_delivery.fn(config, delivery, db, mock_gh)
        assert result == "acknowledged"

    async def test_retry_skips_when_session_acknowledged(self, db, config):
        """Retry skips when fallback session (not target_entity) acknowledged."""
        # Delivery was for coding-agent but fell back to ike session
        await db.record_delivery(154, "coding-agent", "ike", "offline")
        # Ike (the session) acknowledged, not coding-agent (the target)
        await db.record_acknowledgment(154, "ike")

        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "coding-agent",
        }

        from agent_backbone.services.routing import retry_delivery

        mock_gh = AsyncMock()
        result = await retry_delivery.fn(config, delivery, db, mock_gh)
        assert result == "acknowledged"

    @patch("agent_backbone.services.routing._flows.safe_deliver", new_callable=AsyncMock)
    async def test_retry_proceeds_when_not_acknowledged(self, mock_deliver, db, config):
        """Retry proceeds to GitHub fetch + delivery when no ack exists."""
        await db.record_delivery(154, "coding-agent", "ike", "offline")
        # No acknowledgment recorded

        # Mock GitHub client to return an open issue
        mock_issue = MagicMock()
        mock_issue.state = "open"
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(return_value=mock_issue)

        mock_deliver.return_value = "delivered"

        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "coding-agent",
        }

        from agent_backbone.services.routing import retry_delivery

        result = await retry_delivery.fn(config, delivery, db, mock_gh)
        assert result == "retried"
        mock_deliver.assert_called_once()

    async def test_retry_skips_same_entity_and_session(self, db, config):
        """When target_entity == session_name, only one ack check needed."""
        await db.record_delivery(42, "ike", "ike", "offline")
        await db.record_acknowledgment(42, "ike")

        delivery = {
            "session_name": "ike",
            "issue_number": 42,
            "target_entity": "ike",
        }

        from agent_backbone.services.routing import retry_delivery

        mock_gh = AsyncMock()
        result = await retry_delivery.fn(config, delivery, db, mock_gh)
        assert result == "acknowledged"

    @patch("agent_backbone.services.routing._flows.safe_deliver", new_callable=AsyncMock)
    async def test_retry_repo_local_issue_fetches_from_repo(self, mock_deliver, db, config):
        await db.record_delivery(77, "agent-backbone", "agent-backbone", "offline")

        mock_issue = MagicMock()
        mock_issue.state = "open"
        mock_issue.repo_full_name = "eandualem/agent-backbone"
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(return_value=mock_issue)
        mock_gh.list_issues = AsyncMock(return_value=[MagicMock(number=77)])
        mock_deliver.return_value = "delivered"

        config.registry.add_repo(RepoInfo(org="WF", name="agent-backbone", path="/some/path"))
        delivery = {
            "session_name": "agent-backbone",
            "issue_number": 77,
            "target_entity": "agent-backbone",
        }

        from agent_backbone.services.routing import retry_delivery

        result = await retry_delivery.fn(config, delivery, db, mock_gh)
        assert result == "retried"
        assert mock_gh.get_issue.await_args.kwargs["repo_full_name"] == "eandualem/agent-backbone"

    @patch("agent_backbone.services.routing._flows.safe_deliver", new_callable=AsyncMock)
    async def test_retry_retries_agent_working_deliveries(self, mock_deliver, db, config):
        await db.record_delivery(88, "coding-agent", "ike", "agent_working")

        mock_issue = MagicMock()
        mock_issue.state = "open"
        mock_issue.repo_full_name = "eandualem/orchestration"
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(return_value=mock_issue)
        mock_gh.list_issues = AsyncMock(return_value=[MagicMock(number=88)])
        mock_deliver.return_value = "delivered"

        delivery = {
            "session_name": "ike",
            "issue_number": 88,
            "target_entity": "coding-agent",
        }

        from agent_backbone.services.routing import retry_delivery

        result = await retry_delivery.fn(config, delivery, db, mock_gh)
        assert result == "retried"
        mock_deliver.assert_called_once()


class TestDeliveryRetryQueueDrain:
    @patch("agent_backbone.services.routing._flows.safe_deliver", new_callable=AsyncMock)
    @patch(
        "agent_backbone.services.routing._flows.list_open_queue_for_target",
        new_callable=AsyncMock,
    )
    @patch("agent_backbone.services.terminal.list_sessions", new_callable=AsyncMock)
    async def test_queue_drain_runs_without_failed_issue_rows(
        self,
        mock_list_sessions,
        mock_list_open_queue_for_target,
        mock_deliver,
        db,
        config,
    ):
        await db.enqueue_message(
            session_name="ike",
            message="Comment payload",
            issue_number=91,
            target_entity="coding-agent",
            delivery_kind="comment",
            flow_name="issue-dispatcher",
        )

        mock_list_sessions.return_value = ["ike"]
        mock_list_open_queue_for_target.return_value = [MagicMock(number=91)]
        mock_deliver.return_value = "delivered"

        from agent_backbone.services._locator import init as init_flow_services
        from agent_backbone.services.routing import delivery_retry

        mock_gh = AsyncMock()
        init_flow_services(config=config, db=db, gh=mock_gh)

        summary = await delivery_retry.fn()

        assert summary["queue_delivered"] == 1
        assert mock_deliver.await_args.kwargs["delivery_kind"] == "comment"
        row = await db.get_message_by_id(1)
        assert row["status"] == "delivered"

    @patch("agent_backbone.services.routing._flows.safe_deliver", new_callable=AsyncMock)
    @patch(
        "agent_backbone.services.routing._flows.list_open_queue_for_target",
        new_callable=AsyncMock,
    )
    @patch("agent_backbone.services.terminal.list_sessions", new_callable=AsyncMock)
    async def test_queue_drain_delivers_direct_messages_without_issue_metadata(
        self,
        mock_list_sessions,
        mock_list_open_queue_for_target,
        mock_deliver,
        db,
        config,
    ):
        await db.enqueue_message(
            session_name="ike",
            message="Direct payload",
            delivery_kind="direct_message",
            flow_name="api-messages",
        )

        mock_list_sessions.return_value = ["ike"]
        mock_deliver.return_value = "delivered"

        from agent_backbone.services._locator import init as init_flow_services
        from agent_backbone.services.routing import delivery_retry

        mock_gh = AsyncMock()
        init_flow_services(config=config, db=db, gh=mock_gh)

        summary = await delivery_retry.fn()

        assert summary["queue_delivered"] == 1
        assert mock_deliver.await_args.kwargs["issue_number"] is None
        assert mock_deliver.await_args.kwargs["target_entity"] is None
        assert mock_deliver.await_args.kwargs["delivery_kind"] == "direct_message"
        mock_list_open_queue_for_target.assert_not_called()
        row = await db.get_message_by_id(1)
        assert row["status"] == "delivered"


class TestPurgePendingForIssue:
    async def test_purges_pending_messages_for_issue(self, db):
        """purge_pending_for_issue marks all pending messages for an issue as delivered (#780)."""
        await db.enqueue_message(
            session_name="feynman",
            message="Comment A",
            issue_number=775,
            target_entity="feynman",
            delivery_kind="comment",
            flow_name="issue-dispatcher",
        )
        await db.enqueue_message(
            session_name="ike",
            message="Comment B",
            issue_number=775,
            target_entity="ike",
            delivery_kind="comment",
            flow_name="issue-dispatcher",
        )
        # Different issue — should not be purged
        await db.enqueue_message(
            session_name="feynman",
            message="Comment C",
            issue_number=776,
            target_entity="feynman",
            delivery_kind="comment",
            flow_name="issue-dispatcher",
        )

        purged = await db.purge_pending_for_issue(775)

        assert purged == 2
        row1 = await db.get_message_by_id(1)
        assert row1["status"] == "delivered"
        row2 = await db.get_message_by_id(2)
        assert row2["status"] == "delivered"
        row3 = await db.get_message_by_id(3)
        assert row3["status"] == "pending"

    async def test_purge_returns_zero_when_no_pending(self, db):
        """Returns 0 when there are no pending messages for the issue."""
        purged = await db.purge_pending_for_issue(999)
        assert purged == 0


class TestDeliveryDedupPrefixedOutcomes:
    """Tests for dedup with prefixed outcomes (comment_delivered, etc.) — #785."""

    async def test_comment_delivered_suppresses_retry(self, db):
        """A comment_delivered outcome should suppress the failed row in get_failed_deliveries."""
        # Record a failed comment delivery, then a successful one
        await db.record_delivery(100, "feynman", "feynman", "comment_offline")
        await db.record_delivery(100, "feynman", "feynman", "comment_delivered")

        failed = await db.get_failed_deliveries(limit=50)
        issue_numbers = [row["issue_number"] for row in failed]
        assert 100 not in issue_numbers

    async def test_unprefixed_delivered_still_suppresses(self, db):
        """Standard delivered outcome still suppresses failed rows."""
        await db.record_delivery(101, "ike", "ike", "offline")
        await db.record_delivery(101, "ike", "ike", "delivered")

        failed = await db.get_failed_deliveries(limit=50)
        issue_numbers = [row["issue_number"] for row in failed]
        assert 101 not in issue_numbers

    async def test_unsuppressed_failure_still_retried(self, db):
        """A failed delivery without any later success still appears for retry."""
        await db.record_delivery(102, "ike", "ike", "offline")

        failed = await db.get_failed_deliveries(limit=50)
        issue_numbers = [row["issue_number"] for row in failed]
        assert 102 in issue_numbers

    @patch(
        "agent_backbone.services.routing._delivery.get_session_intelligence",
        new_callable=AsyncMock,
    )
    @patch(
        "agent_backbone.services.routing._delivery.send_message",
        new_callable=AsyncMock,
    )
    async def test_safe_deliver_dedup_applies_to_comments(
        self, mock_send, mock_intel, db, config
    ):
        """safe_deliver suppresses duplicate comment delivery when a success exists."""
        # Record a prior successful comment delivery
        await db.record_delivery(200, "feynman", "feynman", "comment_delivered")

        from agent_backbone.services.routing._delivery import safe_deliver

        outcome = await safe_deliver(
            "feynman",
            "Duplicate comment",
            config,
            db=db,
            issue_number=200,
            target_entity="feynman",
            flow_name="test",
            delivery_kind="comment",
        )

        assert outcome == "already_delivered"
        mock_send.assert_not_called()
