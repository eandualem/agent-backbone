"""Tests for the GitHub webhook route at api/routes/webhook.py."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.api.deps import get_delivery_service, get_dispatch_service
from agent_backbone.config import AgentsConfig, AgentSpec
from tests.conftest import TEST_REPO

WEBHOOK_PATH = "/webhooks/github"


def _make_signature(payload: bytes, secret: str = "test-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _webhook_headers(
    payload: bytes,
    *,
    secret: str = "test-secret",
    event: str = "issues",
    delivery_id: str = "unique-delivery-1",
    signature: str | None = None,
) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": (
            signature if signature is not None else _make_signature(payload, secret)
        ),
        "X-GitHub-Delivery": delivery_id,
        "Content-Type": "application/json",
    }


@pytest.fixture
def mock_delivery_svc():
    svc = MagicMock()
    svc.is_recent_notification = MagicMock(return_value=False)
    return svc


@pytest.fixture
def mock_dispatch_svc():
    svc = MagicMock()
    svc.on_issue_closed = AsyncMock(return_value={"feynman": "delivered_#11"})
    svc.issue_dispatcher = AsyncMock(
        return_value=MagicMock(delivered=["ike"], offline=[], deferred=[])
    )
    return svc


@pytest.fixture(autouse=True)
def _override_services(api_app, mock_delivery_svc, mock_dispatch_svc):
    api_app.dependency_overrides[get_delivery_service] = lambda: mock_delivery_svc
    api_app.dependency_overrides[get_dispatch_service] = lambda: mock_dispatch_svc
    yield
    api_app.dependency_overrides.pop(get_delivery_service, None)
    api_app.dependency_overrides.pop(get_dispatch_service, None)


class TestHealthEndpoint:
    async def test_health_returns_lifecycle_health(self, api_client):
        resp = await api_client.get("/health")
        assert resp.status_code == 200
        assert "healthy" in resp.json()


class TestWebhookSignatureValidation:
    async def test_valid_signature_accepted(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        resp = await api_client.post(
            WEBHOOK_PATH, content=payload_bytes, headers=_webhook_headers(payload_bytes)
        )
        assert resp.status_code == 200
        assert resp.text == "dispatch: 1 delivered, 0 offline, 0 deferred"

    async def test_invalid_signature_returns_403(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, signature="sha256=invalid")
        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)
        assert resp.status_code == 403
        assert resp.text == "Invalid signature"

    async def test_missing_signature_returns_403(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "no-sig"}
        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)
        assert resp.status_code == 403

    async def test_missing_secret_configuration_rejects(self, api_client, api_app, webhook_payload):
        api_app.state.config = replace(api_app.state.config, webhook_secret="")
        payload_bytes = json.dumps(webhook_payload).encode()
        resp = await api_client.post(
            WEBHOOK_PATH, content=payload_bytes, headers=_webhook_headers(payload_bytes)
        )
        assert resp.status_code == 403
        assert resp.text == "Webhook secret not configured"

    async def test_ping_event(self, api_client):
        payload_bytes = json.dumps({"zen": "Keep it simple"}).encode()
        resp = await api_client.post(
            WEBHOOK_PATH,
            content=payload_bytes,
            headers=_webhook_headers(payload_bytes, event="ping", delivery_id="ping-1"),
        )
        assert resp.status_code == 200
        assert resp.text == "pong"


class TestWebhookDeduplication:
    async def test_duplicate_delivery_id_returns_200_skipped(
        self, api_client, webhook_payload, mock_dispatch_svc
    ):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dup-delivery-abc")

        resp1 = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)
        resp2 = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp2.text == "Duplicate, skipped"
        assert mock_dispatch_svc.issue_dispatcher.await_count == 1

    async def test_issue_event_recent_notification_returns_deduped(
        self, api_client, webhook_payload, mock_delivery_svc, mock_dispatch_svc
    ):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="recent-issue-dedup")
        mock_delivery_svc.is_recent_notification.return_value = True

        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)

        assert resp.text == "deduped: all targets already notified for #42"
        mock_dispatch_svc.issue_dispatcher.assert_not_awaited()

    async def test_comment_event_bypasses_recent_notification_dedup(
        self, api_client, mock_delivery_svc, mock_dispatch_svc
    ):
        payload = {
            "action": "created",
            "repository": {"full_name": TEST_REPO},
            "issue": {
                "number": 730,
                "title": "[task] Something",
                "state": "open",
                "labels": [{"name": "from:ike"}, {"name": "for:feynman"}, {"name": "task"}],
            },
            "comment": {"id": 1, "body": "[from:feynman]\nAcknowledged.", "user": {"login": "x"}},
        }
        payload_bytes = json.dumps(payload).encode()
        headers = _webhook_headers(payload_bytes, event="issue_comment", delivery_id="c-1")
        mock_delivery_svc.is_recent_notification.return_value = True

        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)

        assert resp.text == "dispatch: 1 delivered, 0 offline, 0 deferred"
        mock_dispatch_svc.issue_dispatcher.assert_awaited_once()
        mock_delivery_svc.is_recent_notification.assert_not_called()

    async def test_comment_on_closed_issue_ignored(self, api_client, mock_dispatch_svc):
        payload = {
            "action": "created",
            "repository": {"full_name": TEST_REPO},
            "issue": {"number": 775, "title": "x", "state": "closed", "labels": []},
            "comment": {"id": 999, "body": "done", "user": {"login": "x"}},
        }
        payload_bytes = json.dumps(payload).encode()
        headers = _webhook_headers(payload_bytes, event="issue_comment", delivery_id="c-closed")

        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)

        assert "ignored: comment on closed issue" in resp.text
        mock_dispatch_svc.issue_dispatcher.assert_not_awaited()


class TestWebhookPayloadParsing:
    async def test_invalid_json_returns_400(self, api_client):
        payload_bytes = b"not valid json {{"
        headers = _webhook_headers(payload_bytes, delivery_id="delivery-bad-json")
        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)
        assert resp.status_code == 400
        assert resp.text == "Invalid JSON"


class TestWebhookDispatch:
    async def test_dispatches_issue_opened_event(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-test-1")

        with patch(
            "agent_backbone.api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        event = mock_dispatch.call_args[0][0]
        assert event.issue.number == 42
        assert event.issue.repo_full_name == TEST_REPO

    async def test_issue_closed_goes_to_lifecycle(self, api_client, mock_dispatch_svc, api_app):
        api_app.state.github = AsyncMock()
        payload = {
            "action": "closed",
            "repository": {"full_name": TEST_REPO},
            "issue": {"number": 10, "title": "x", "state": "closed", "labels": []},
        }
        payload_bytes = json.dumps(payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="closed-1")

        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)

        assert resp.text.startswith("lifecycle:")
        mock_dispatch_svc.on_issue_closed.assert_awaited_once()

    async def test_issue_closed_without_github_is_ignored(self, api_client, mock_dispatch_svc):
        payload = {
            "action": "closed",
            "issue": {"number": 10, "title": "x", "state": "closed", "labels": []},
        }
        payload_bytes = json.dumps(payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="closed-2")

        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)

        assert "github client not configured" in resp.text
        mock_dispatch_svc.on_issue_closed.assert_not_awaited()

    async def test_pull_request_event_dispatches_for_repo_owner(
        self, api_client, api_app, mock_dispatch_svc
    ):
        api_app.state.config = replace(
            api_app.state.config,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        payload = {
            "action": "opened",
            "repository": {"full_name": "acme/backbone"},
            "pull_request": {
                "number": 73,
                "title": "Add multi-repo webhook support",
                "state": "open",
                "html_url": "https://github.com/acme/backbone/pull/73",
                "labels": [],
            },
        }
        payload_bytes = json.dumps(payload).encode()
        headers = _webhook_headers(payload_bytes, event="pull_request", delivery_id="pr-1")

        resp = await api_client.post(WEBHOOK_PATH, content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        event = mock_dispatch_svc.issue_dispatcher.await_args.args[0]
        assert event.event_type.value == "pull_request_opened"
        assert event.issue.repo_full_name == "acme/backbone"


class TestIntegrationReply:
    async def test_requires_auth(self, api_client):
        resp = await api_client.post(
            "/api/integrations/reply", json={"session": "ike", "text": "hi"}
        )
        assert resp.status_code == 401

    async def test_without_any_integration_returns_503(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/integrations/reply", json={"session": "ike", "text": "hi"}, headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_unregistered_agent_404(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/integrations/reply", json={"session": "stray", "text": "hi"}, headers=auth_headers
        )
        assert resp.status_code == 404
