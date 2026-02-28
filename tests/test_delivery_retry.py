"""Tests for flows/delivery_retry.py — ack check in retry flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.services.persistence import BackboneDB


@pytest.fixture
async def db():
    db = BackboneDB("sqlite+aiosqlite:///:memory:")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


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

        from agent_backbone.services.delivery import retry_delivery

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

        from agent_backbone.services.delivery import retry_delivery

        mock_gh = AsyncMock()
        result = await retry_delivery.fn(config, delivery, db, mock_gh)
        assert result == "acknowledged"

    @patch("agent_backbone.services.delivery._flows.safe_deliver", new_callable=AsyncMock)
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

        from agent_backbone.services.delivery import retry_delivery

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

        from agent_backbone.services.delivery import retry_delivery

        mock_gh = AsyncMock()
        result = await retry_delivery.fn(config, delivery, db, mock_gh)
        assert result == "acknowledged"
