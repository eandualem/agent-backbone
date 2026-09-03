"""Tests for api/routes/messages.py -- inter-agent messaging endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.routing import DeliveryReport

# ---------------------------------------------------------------------------
# POST /api/messages
# ---------------------------------------------------------------------------


class TestSendMessage:
    async def test_send_message_delivered(self, api_client, auth_headers, api_app):
        """Returns ok=True when safe_deliver returns 'delivered'."""
        with patch(
            "agent_backbone.api.routes.messages.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.DELIVERED),
        ):
            resp = await api_client.post(
                "/api/messages",
                headers=auth_headers,
                json={
                    "target_session": "ike",
                    "from_entity": "bell",
                    "message": "Please check issue #42",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "ike"
        assert data["outcome"] == "delivered"

    async def test_unregistered_target_is_never_typed_into(self, api_client, auth_headers):
        with patch("agent_backbone.api.routes.messages.deliver", new_callable=AsyncMock) as deliver:
            resp = await api_client.post(
                "/api/messages",
                headers=auth_headers,
                json={"target_session": "stray", "from_entity": "bell", "message": "hi"},
            )
        assert resp.status_code == 404
        assert "not a registered agent" in resp.json()["detail"]
        deliver.assert_not_called()

    async def test_send_message_agent_working(self, api_client, auth_headers, api_app):
        """Returns ok=False when agent is busy (outcome != 'delivered')."""
        with patch(
            "agent_backbone.api.routes.messages.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.AGENT_WORKING, "stored"),
        ):
            resp = await api_client.post(
                "/api/messages",
                headers=auth_headers,
                json={
                    "target_session": "ike",
                    "from_entity": "bell",
                    "message": "Check this",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["outcome"] == "agent_working"
        assert data["queued"] is True and data["queue"] == "stored"
        assert data["detail"].startswith("Queued: ike is agent working")

    async def test_same_message_already_waiting_is_said_in_words(
        self, api_client, auth_headers, api_app
    ):
        with patch(
            "agent_backbone.api.routes.messages.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.AGENT_WORKING, "already_queued"),
        ):
            resp = await api_client.post(
                "/api/messages",
                headers=auth_headers,
                json={"target_session": "ike", "from_entity": "bell", "message": "Check this"},
            )
        data = resp.json()
        assert data["queued"] is True and data["queue"] == "already_queued"
        assert data["detail"] == (
            "Already in the queue: the same message from you is waiting for ike. "
            "It was not added again."
        )

    async def test_storage_failure_is_never_called_queued(self, api_client, auth_headers, api_app):
        with patch(
            "agent_backbone.api.routes.messages.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.AGENT_WORKING, "failed"),
        ):
            resp = await api_client.post(
                "/api/messages",
                headers=auth_headers,
                json={"target_session": "ike", "from_entity": "bell", "message": "Check this"},
            )
        data = resp.json()
        assert data["queued"] is False and data["queue"] == "failed"
        assert "not queued" in data["detail"] and "Send it again later" in data["detail"]

    async def test_sender_is_part_of_the_queue_identity(self, api_client, auth_headers, api_app):
        mock_deliver = AsyncMock(return_value=DeliveryReport(DeliveryOutcome.DELIVERED))
        with patch("agent_backbone.api.routes.messages.deliver", mock_deliver):
            await api_client.post(
                "/api/messages",
                headers=auth_headers,
                json={"target_session": "ike", "from_entity": "bell", "message": "hi"},
            )
        assert mock_deliver.call_args.kwargs["sender"] == "bell"

    async def test_send_message_formats_envelope(self, api_client, auth_headers, api_app):
        """Message is wrapped with [via:backbone from:{entity}] envelope."""
        mock_deliver = AsyncMock(return_value=DeliveryReport(DeliveryOutcome.DELIVERED))
        with patch("agent_backbone.api.routes.messages.deliver", mock_deliver):
            await api_client.post(
                "/api/messages",
                headers=auth_headers,
                json={
                    "target_session": "feynman",
                    "from_entity": "ike",
                    "message": "Hello there",
                },
            )

        mock_deliver.assert_awaited_once()
        call_kwargs = mock_deliver.call_args
        assert call_kwargs.kwargs["session_name"] == "feynman"
        delivered_msg = call_kwargs.kwargs["message"]
        assert delivered_msg == "[via:backbone from:ike] Hello there"
        assert call_kwargs.kwargs["source"] == "api-messages"
        assert call_kwargs.kwargs["delivery_kind"] == "direct_message"

    async def test_requires_auth(self, api_client, api_key):
        """Request without auth headers is rejected."""
        resp = await api_client.post(
            "/api/messages",
            json={
                "target_session": "ike",
                "from_entity": "bell",
                "message": "test",
            },
        )
        assert resp.status_code == 401

    async def test_missing_fields(self, api_client, auth_headers, api_app):
        """Incomplete body returns 422."""
        resp = await api_client.post(
            "/api/messages",
            headers=auth_headers,
            json={"target_session": "ike"},
        )
        assert resp.status_code == 422

    async def test_priority_passed_to_safe_deliver(self, api_client, auth_headers, api_app):
        """Priority flag is forwarded to safe_deliver."""
        mock_deliver = AsyncMock(return_value=DeliveryReport(DeliveryOutcome.DELIVERED))
        with patch("agent_backbone.api.routes.messages.deliver", mock_deliver):
            await api_client.post(
                "/api/messages",
                headers=auth_headers,
                json={
                    "target_session": "ike",
                    "from_entity": "bell",
                    "message": "urgent",
                    "priority": True,
                },
            )

        assert mock_deliver.call_args.kwargs["priority"] is True
