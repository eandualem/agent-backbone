"""Tests for the FastAPI webhook route at api/routes/webhook.py."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from agent_backbone.services.registry import RepoInfo


def _make_signature(payload: bytes, secret: str = "test-secret") -> str:
    """Generate a valid HMAC-SHA256 signature for the given payload."""
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _webhook_headers(
    payload: bytes,
    *,
    secret: str = "test-secret",
    event: str = "issues",
    delivery_id: str = "unique-delivery-1",
    signature: str | None = None,
) -> dict[str, str]:
    """Build standard GitHub webhook headers."""
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": (
            signature if signature is not None else _make_signature(payload, secret)
        ),
        "X-GitHub-Delivery": delivery_id,
        "Content-Type": "application/json",
    }


class TestHealthEndpoint:
    async def test_health_returns_lifecycle_health(self, api_client):
        resp = await api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "healthy" in data


class TestReplyRouting:
    async def test_requires_auth(self, api_client, api_key):
        resp = await api_client.post("/api/reply", json={"session": "ike", "text": "hello"})

        assert resp.status_code == 401

    async def test_reaches_handler_with_auth(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/reply",
            content=b"not valid json {{",
            headers=auth_headers,
        )

        assert resp.status_code == 400
        assert resp.text == "Invalid JSON"


class TestWebhookSignatureValidation:
    def setup_method(self):
        self._clear_dedup = True

    async def test_valid_signature_accepted(self, api_client, api_app, webhook_payload):
        if self._clear_dedup:
            api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes)

        with patch(
            "agent_backbone.api.routes.webhook.emit_run_event",
            new_callable=AsyncMock,
        ):
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "accepted"

    async def test_github_app_secret_is_accepted(self, api_client, api_app, webhook_payload):
        api_app.state.db._seen_deliveries.clear()
        api_app.state.config = replace(
            api_app.state.config,
            github_app_webhook_secret="app-secret",
        )
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(
            payload_bytes,
            secret="app-secret",
            delivery_id="delivery-app-secret",
        )

        with patch(
            "agent_backbone.api.routes.webhook.emit_run_event",
            new_callable=AsyncMock,
        ):
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "accepted"

    async def test_invalid_signature_returns_403(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, signature="sha256=invalid")

        resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 403
        assert resp.text == "Invalid signature"

    async def test_missing_signature_returns_403(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-no-sig",
            "Content-Type": "application/json",
        }

        resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 403
        assert resp.text == "Invalid signature"


class TestWebhookDeduplication:
    async def test_duplicate_delivery_id_returns_200_skipped(
        self, api_client, api_app, webhook_payload
    ):
        api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        delivery_id = "dup-delivery-abc"
        headers = _webhook_headers(payload_bytes, delivery_id=delivery_id)

        # First request: accepted
        with patch(
            "agent_backbone.api.routes.webhook.emit_run_event",
            new_callable=AsyncMock,
        ):
            resp1 = await api_client.post("/", content=payload_bytes, headers=headers)
        assert resp1.status_code == 200
        assert resp1.text == "accepted"

        # Second request with same delivery ID: deduped
        resp2 = await api_client.post("/", content=payload_bytes, headers=headers)
        assert resp2.status_code == 200
        assert resp2.text == "Duplicate, skipped"

class TestWebhookPayloadParsing:
    async def test_invalid_json_returns_400(self, api_client, api_app):
        api_app.state.db._seen_deliveries.clear()
        payload_bytes = b"not valid json {{"
        headers = _webhook_headers(payload_bytes, delivery_id="delivery-bad-json")

        resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 400
        assert resp.text == "Invalid JSON"


class TestWebhookDispatch:
    async def test_dispatches_issue_opened_event_on_root_path(
        self, api_client, api_app, webhook_payload
    ):
        api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-root-test-1")

        with patch(
            "agent_backbone.api.routes.webhook.emit_run_event",
            new_callable=AsyncMock,
        ):
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "accepted"

    async def test_dispatches_issue_opened_event(self, api_client, api_app, webhook_payload):
        api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-test-1")

        with patch(
            "agent_backbone.api.routes.webhook.emit_run_event",
            new_callable=AsyncMock,
        ) as mock_emit:
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "accepted"
        # Verify run event was emitted for issue.created
        mock_emit.assert_awaited()
        event_types = [call.args[0] for call in mock_emit.await_args_list]
        assert "issue.created" in event_types

    async def test_dispatches_non_default_repo_event(self, api_client, api_app, webhook_payload):
        api_app.state.db._seen_deliveries.clear()
        repo_payload = dict(webhook_payload)
        repo_payload["repository"] = {"full_name": "WF/agent-shell"}
        repo_payload["issue"] = {
            **webhook_payload["issue"],
            "html_url": "https://github.com/WF/agent-shell/issues/42",
        }
        payload_bytes = json.dumps(repo_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-test-non-default")

        with patch(
            "agent_backbone.api.routes.webhook.emit_run_event",
            new_callable=AsyncMock,
        ):
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "accepted"

    async def test_dispatch_outcome_returned_in_response(
        self, api_client, api_app, webhook_payload
    ):
        api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-test-2")

        with patch(
            "agent_backbone.api.routes.webhook.emit_run_event",
            new_callable=AsyncMock,
        ):
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "accepted"

    async def test_pull_request_event_dispatches_for_repo_session(self, api_client, api_app):
        api_app.state.db._seen_deliveries.clear()
        api_app.state.config.registry.add_repo(
            RepoInfo(org="WF", name="agent-backbone", path="/some/path/agent-backbone")
        )
        payload = {
            "action": "opened",
            "repository": {"full_name": "eandualem/agent-backbone"},
            "pull_request": {
                "number": 73,
                "title": "Add multi-repo webhook support",
                "state": "open",
                "html_url": "https://github.com/eandualem/agent-backbone/pull/73",
                "labels": [],
            },
        }
        payload_bytes = json.dumps(payload).encode()
        headers = _webhook_headers(
            payload_bytes,
            event="pull_request",
            delivery_id="dispatch-pr-test-1",
        )

        with patch(
            "agent_backbone.api.routes.webhook.emit_run_event",
            new_callable=AsyncMock,
        ) as mock_emit:
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "accepted"
        # Verify run event was emitted for pr.opened
        mock_emit.assert_awaited()
        event_types = [call.args[0] for call in mock_emit.await_args_list]
        assert "pr.opened" in event_types
