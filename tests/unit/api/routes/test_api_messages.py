"""Tests for api/routes/messages.py -- inter-agent messaging endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# POST /api/messages
# ---------------------------------------------------------------------------


class TestSendMessage:
    async def test_send_message_delivered(self, api_client, auth_headers, api_app):
        """Returns ok=True when deliver_message returns 'delivered'."""
        with patch(
            "agent_backbone.api.routes.messages.deliver_message",
            new_callable=AsyncMock,
            return_value="delivered",
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

    async def test_send_message_agent_working(self, api_client, auth_headers, api_app):
        """Returns ok=False when agent is busy (outcome != 'delivered')."""
        with patch(
            "agent_backbone.api.routes.messages.deliver_message",
            new_callable=AsyncMock,
            return_value="agent_working",
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

    async def test_send_message_formats_envelope(self, api_client, auth_headers, api_app):
        """Message is wrapped with [via:backbone from:{entity}] envelope."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.messages.deliver_message", mock_deliver):
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
        call_args = mock_deliver.call_args
        # deliver_message(session_name, message, config, *, priority=False)
        assert call_args[0][0] == "feynman"
        assert call_args[0][1] == "[via:backbone from:ike] Hello there"

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

    async def test_priority_passed_to_deliver_message(self, api_client, auth_headers, api_app):
        """Priority flag is forwarded to deliver_message."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.messages.deliver_message", mock_deliver):
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
