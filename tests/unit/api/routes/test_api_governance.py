"""Tests for api/routes/governance.py — governance action execution."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch


class TestGovernanceActions:
    async def test_notify_agent_delivered(self, api_client, auth_headers, api_app):
        """notify_agent action calls safe_deliver and returns result."""
        with patch(
            "agent_backbone.services.routing._delivery.safe_deliver",
            new_callable=AsyncMock,
            return_value="delivered",
        ):
            resp = await api_client.post(
                "/api/governance/actions",
                headers=auth_headers,
                json={
                    "action_type": "notify_agent",
                    "params": {"session": "agent-backbone", "message": "Hello"},
                    "track_context": {"track_id": "test"},
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["action_type"] == "notify_agent"
        assert data["result"]["outcome"] == "delivered"

    async def test_notify_elias_sends_telegram(self, api_client, auth_headers, api_app):
        """notify_elias action sends Telegram notification."""
        with patch(
            "agent_backbone.services.telegram.interface.TelegramService.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ):
            resp = await api_client.post(
                "/api/governance/actions",
                headers=auth_headers,
                json={
                    "action_type": "notify_elias",
                    "params": {"message": "Alert!"},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_auto_comment(self, api_client, auth_headers, api_app):
        """auto_comment action calls gh.add_comment."""
        mock_gh = AsyncMock()
        mock_gh.add_comment = AsyncMock(return_value=None)
        api_app.state.github = mock_gh
        resp = await api_client.post(
            "/api/governance/actions",
            headers=auth_headers,
            json={
                "action_type": "auto_comment",
                "params": {"issue_number": 42, "body": "Automated comment"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_gh.add_comment.assert_awaited_once()

    async def test_log_action(self, api_client, auth_headers, api_app):
        """log action returns ok without external calls."""
        resp = await api_client.post(
            "/api/governance/actions",
            headers=auth_headers,
            json={
                "action_type": "log",
                "params": {"message": "Test log entry"},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["result"]["logged"] is True

    async def test_semantic_search_stub(self, api_client, auth_headers, api_app):
        """semantic_search returns stub response."""
        resp = await api_client.post(
            "/api/governance/actions",
            headers=auth_headers,
            json={
                "action_type": "semantic_search",
                "params": {"query": "test"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["result"]["stub"] is True

    async def test_unknown_action_type(self, api_client, auth_headers, api_app):
        """Unknown action_type returns ok=False with error."""
        resp = await api_client.post(
            "/api/governance/actions",
            headers=auth_headers,
            json={
                "action_type": "nonexistent",
                "params": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "error" in data["result"]

    async def test_emits_action_executed_event(self, api_client, auth_headers, api_app):
        """Successful action emits action.executed governance event."""
        with patch(
            "agent_backbone.api.governance_events.emit_governance_event",
            new_callable=AsyncMock,
        ) as mock_emit:
            resp = await api_client.post(
                "/api/governance/actions",
                headers=auth_headers,
                json={
                    "action_type": "log",
                    "params": {"message": "test"},
                    "track_context": {"track_id": "bug-fix", "instance_id": "inst_1"},
                },
            )
        assert resp.status_code == 200
        # Find the action.executed call
        found = False
        for call in mock_emit.await_args_list:
            if call.args[0] == "action.executed":
                found = True
                break
        assert found, "action.executed governance event was not emitted"

    async def test_requires_auth(self, api_client, api_key):
        """Request without auth headers is rejected."""
        resp = await api_client.post(
            "/api/governance/actions",
            json={"action_type": "log", "params": {}},
        )
        assert resp.status_code == 401
