"""Tests for flows/delivery_retry.py — ack check in retry flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.persistence import BackboneDB


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    async with BackboneDB(db_path) as database:
        yield database, db_path


class TestRetryDeliveryAckCheck:
    """Tests for the acknowledgment check added to retry_delivery."""

    async def test_retry_skips_acknowledged_target_entity(self, db, config):
        """Retry skips delivery when target_entity has acknowledged the issue."""
        database, db_path = db

        # Record a failed delivery and an ack for the target entity
        await database.record_delivery(154, "coding-agent", "ike", "offline")
        await database.record_acknowledgment(154, "coding-agent")

        # Patch config to use our test DB
        config_with_db = _config_with_db(config, db_path)

        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "coding-agent",
        }

        from flows.delivery_retry import retry_delivery

        result = await retry_delivery.fn(config_with_db, delivery)
        assert result == "acknowledged"

    async def test_retry_skips_when_session_acknowledged(self, db, config):
        """Retry skips when fallback session (not target_entity) acknowledged."""
        database, db_path = db

        # Delivery was for coding-agent but fell back to ike session
        await database.record_delivery(154, "coding-agent", "ike", "offline")
        # Ike (the session) acknowledged, not coding-agent (the target)
        await database.record_acknowledgment(154, "ike")

        config_with_db = _config_with_db(config, db_path)

        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "coding-agent",
        }

        from flows.delivery_retry import retry_delivery

        result = await retry_delivery.fn(config_with_db, delivery)
        assert result == "acknowledged"

    @patch("flows.delivery_retry.safe_deliver", new_callable=AsyncMock)
    @patch("flows.delivery_retry.GitHubClient")
    async def test_retry_proceeds_when_not_acknowledged(
        self, mock_gh_cls, mock_deliver, db, config
    ):
        """Retry proceeds to GitHub fetch + delivery when no ack exists."""
        database, db_path = db

        await database.record_delivery(154, "coding-agent", "ike", "offline")
        # No acknowledgment recorded

        config_with_db = _config_with_db(config, db_path)

        # Mock GitHub client to return an open issue
        mock_issue = MagicMock()
        mock_issue.state = "open"
        mock_gh = AsyncMock()
        mock_gh.get_issue = AsyncMock(return_value=mock_issue)
        mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
        mock_gh.__aexit__ = AsyncMock(return_value=False)
        mock_gh_cls.return_value = mock_gh

        mock_deliver.return_value = "delivered"

        delivery = {
            "session_name": "ike",
            "issue_number": 154,
            "target_entity": "coding-agent",
        }

        from flows.delivery_retry import retry_delivery

        result = await retry_delivery.fn(config_with_db, delivery)
        assert result == "retried"
        mock_deliver.assert_called_once()

    async def test_retry_skips_same_entity_and_session(self, db, config):
        """When target_entity == session_name, only one ack check needed."""
        database, db_path = db

        await database.record_delivery(42, "ike", "ike", "offline")
        await database.record_acknowledgment(42, "ike")

        config_with_db = _config_with_db(config, db_path)

        delivery = {
            "session_name": "ike",
            "issue_number": 42,
            "target_entity": "ike",
        }

        from flows.delivery_retry import retry_delivery

        result = await retry_delivery.fn(config_with_db, delivery)
        assert result == "acknowledged"


def _config_with_db(config, db_path: str):
    """Create a config copy pointing to the test DB."""
    from dataclasses import replace

    from src.config import DeliveryConfig

    return replace(config, delivery=DeliveryConfig(db_path=db_path))
