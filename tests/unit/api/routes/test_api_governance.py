"""Tests for api/routes/governance.py — run action execution."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch


class TestRunActions:
    async def test_notify_agent_delivered(self, api_client, auth_headers, api_app):
        """notify_agent action calls safe_deliver and returns result."""
        with patch(
            "agent_backbone.services.messaging.deliver_message",
            new_callable=AsyncMock,
            return_value="delivered",
        ):
            resp = await api_client.post(
                "/api/runs/actions",
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
                "/api/runs/actions",
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
            "/api/runs/actions",
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
            "/api/runs/actions",
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
            "/api/runs/actions",
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
            "/api/runs/actions",
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
        """Successful action emits action.executed run event."""
        with patch(
            "agent_backbone.api.run_events.emit_run_event",
            new_callable=AsyncMock,
        ) as mock_emit:
            resp = await api_client.post(
                "/api/runs/actions",
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
        assert found, "action.executed run event was not emitted"

    async def test_notify_agent_resolves_entity_from_track_context(
        self, api_client, auth_headers, api_app
    ):
        """notify_agent resolves abstract entity name via track_context org."""
        from dataclasses import replace as dc_replace

        from agent_backbone.services.registry import EntityEntry, EntityRegistry

        old_config = api_app.state.config
        registry = EntityRegistry(
            entities={
                **old_config.registry.entities,
                "snow-wf": EntityEntry(
                    session="snow-wf",
                    home="~/ws/core/quality/WF/snow",
                    groups=["validators"],
                    figure="John Snow",
                    role="Bug Validator",
                    organization="WF",
                    entity_type="role-instance",
                    role_entity="snow",
                ),
            },
            repos=old_config.registry.repos,
        )
        api_app.state.config = dc_replace(old_config, registry=registry)

        try:
            with patch(
                "agent_backbone.services.messaging.deliver_message",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver:
                resp = await api_client.post(
                    "/api/runs/actions",
                    headers=auth_headers,
                    json={
                        "action_type": "notify_agent",
                        "params": {"entity": "snow", "message": "Test"},
                        "track_context": {"org": "WF"},
                    },
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            mock_deliver.assert_awaited_once()
            assert mock_deliver.call_args[0][0] == "snow-wf"
        finally:
            api_app.state.config = old_config

    async def test_notify_agent_fails_without_session_or_entity(
        self, api_client, auth_headers, api_app
    ):
        """notify_agent fails gracefully when neither session nor entity resolves."""
        resp = await api_client.post(
            "/api/runs/actions",
            headers=auth_headers,
            json={
                "action_type": "notify_agent",
                "params": {"message": "Test"},
                "track_context": {},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "error" in data["result"]

    async def test_auto_comment_with_message_alias(
        self, api_client, auth_headers, api_app
    ):
        """auto_comment accepts 'message' as alias for 'body'."""
        mock_gh = AsyncMock()
        mock_gh.add_comment = AsyncMock(return_value=None)
        api_app.state.github = mock_gh
        resp = await api_client.post(
            "/api/runs/actions",
            headers=auth_headers,
            json={
                "action_type": "auto_comment",
                "params": {"message": "Governance comment"},
                "track_context": {"issue_id": 99, "repo": "eandualem/test"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_gh.add_comment.assert_awaited_once_with(
            99, "Governance comment", repo_full_name="eandualem/test"
        )

    async def test_auto_comment_derives_issue_from_track_context(
        self, api_client, auth_headers, api_app
    ):
        """auto_comment derives issue_number and repo from track_context."""
        mock_gh = AsyncMock()
        mock_gh.add_comment = AsyncMock(return_value=None)
        api_app.state.github = mock_gh
        resp = await api_client.post(
            "/api/runs/actions",
            headers=auth_headers,
            json={
                "action_type": "auto_comment",
                "params": {"body": "Comment text"},
                "track_context": {"issue_id": 77, "repo": "eandualem/orchestration"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_gh.add_comment.assert_awaited_once_with(
            77, "Comment text", repo_full_name="eandualem/orchestration"
        )

    async def test_requires_auth(self, api_client, api_key):
        """Request without auth headers is rejected."""
        resp = await api_client.post(
            "/api/runs/actions",
            json={"action_type": "log", "params": {}},
        )
        assert resp.status_code == 401
