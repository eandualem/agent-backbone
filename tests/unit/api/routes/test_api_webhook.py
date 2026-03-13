"""Tests for the FastAPI webhook route at api/routes/webhook.py."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.api.deps import get_delivery_service, get_dispatch_service
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


@pytest.fixture
def mock_delivery_svc():
    """Mock DeliveryService for DI override."""
    svc = MagicMock()
    svc.is_recent_notification = MagicMock(return_value=False)
    return svc


@pytest.fixture
def mock_dispatch_svc():
    """Mock DispatchService for DI override."""
    svc = MagicMock()
    svc.on_issue_closed = AsyncMock(return_value="delivered_next")
    svc.issue_dispatcher = AsyncMock()
    return svc


@pytest.fixture(autouse=True)
def _override_services(api_app, mock_delivery_svc, mock_dispatch_svc):
    """Override DI providers with mocks for all webhook tests."""
    api_app.dependency_overrides[get_delivery_service] = lambda: mock_delivery_svc
    api_app.dependency_overrides[get_dispatch_service] = lambda: mock_dispatch_svc
    yield
    api_app.dependency_overrides.pop(get_delivery_service, None)
    api_app.dependency_overrides.pop(get_dispatch_service, None)


class TestHealthEndpoint:
    async def test_health_returns_lifecycle_health(self, api_client):
        resp = await api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "healthy" in data


class TestWebhookSignatureValidation:
    def setup_method(self):
        self._clear_dedup = True

    async def test_valid_signature_accepted(self, api_client, api_app, webhook_payload):
        if self._clear_dedup:
            api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes)

        with patch(
            "agent_backbone.api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        mock_dispatch.assert_awaited_once()

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
            "agent_backbone.api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        mock_dispatch.assert_awaited_once()

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

        # First request: dispatches normally
        with patch(
            "agent_backbone.api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp1 = await api_client.post("/", content=payload_bytes, headers=headers)
        assert resp1.status_code == 200
        mock_dispatch.assert_awaited_once()

        # Second request with same delivery ID: deduped
        resp2 = await api_client.post("/", content=payload_bytes, headers=headers)
        assert resp2.status_code == 200
        assert resp2.text == "Duplicate, skipped"

    async def test_issue_event_recent_notification_returns_deduped(
        self, api_client, api_app, webhook_payload, mock_delivery_svc, mock_dispatch_svc
    ):
        api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="recent-issue-dedup")
        mock_delivery_svc.is_recent_notification.return_value = True

        resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "deduped: all targets already notified for #42"
        mock_dispatch_svc.issue_dispatcher.assert_not_awaited()

    async def test_comment_event_bypasses_recent_notification_dedup(
        self, api_client, api_app, mock_delivery_svc, mock_dispatch_svc
    ):
        api_app.state.db._seen_deliveries.clear()
        payload = {
            "action": "created",
            "repository": {"full_name": "eandualem/orchestration"},
            "issue": {
                "number": 730,
                "title": "[task] orchestration: Git autonomy for coding agents",
                "state": "open",
                "html_url": "https://github.com/eandualem/orchestration/issues/730",
                "labels": [
                    {"name": "from:ike"},
                    {"name": "for:feynman"},
                    {"name": "task"},
                    {"name": "blocking"},
                ],
            },
            "comment": {
                "id": 1,
                "body": "[from:feynman]\nAcknowledged.",
                "user": {"login": "eandualem"},
            },
        }
        payload_bytes = json.dumps(payload).encode()
        headers = _webhook_headers(
            payload_bytes,
            event="issue_comment",
            delivery_id="comment-dedup-regression",
        )
        mock_delivery_svc.is_recent_notification.return_value = True
        mock_dispatch_svc.issue_dispatcher.return_value = MagicMock(
            delivered=["ike"],
            offline=[],
            deferred=[],
        )

        resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "dispatch: 1 delivered, 0 offline, 0 deferred"
        mock_dispatch_svc.issue_dispatcher.assert_awaited_once()
        mock_delivery_svc.is_recent_notification.assert_not_called()


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
            "agent_backbone.api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert "dispatch" in resp.text
        mock_dispatch.assert_awaited_once()

    async def test_dispatches_issue_opened_event(self, api_client, api_app, webhook_payload):
        api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-test-1")

        with patch(
            "agent_backbone.api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert "dispatch" in resp.text
        # Verify the event was passed to dispatch
        call_args = mock_dispatch.call_args
        event = call_args[0][0]
        assert event.issue.number == 42
        assert event.issue.repo_full_name == "eandualem/orchestration"

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
            "agent_backbone.api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        event = mock_dispatch.call_args[0][0]
        assert event.issue.repo_full_name == "WF/agent-shell"

    async def test_dispatch_outcome_returned_in_response(
        self, api_client, api_app, webhook_payload
    ):
        api_app.state.db._seen_deliveries.clear()
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-test-2")

        with patch(
            "agent_backbone.api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "ignored: unknown"
            resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "ignored: unknown"

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

        mock_dispatch = api_app.dependency_overrides[get_dispatch_service]()
        mock_dispatch.issue_dispatcher.return_value = MagicMock(
            delivered=["agent-backbone"],
            offline=[],
            deferred=[],
        )

        resp = await api_client.post("/", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "dispatch: 1 delivered, 0 offline, 0 deferred"
        event = mock_dispatch.issue_dispatcher.await_args.args[0]
        assert event.event_type.value == "pull_request_opened"
        assert event.issue.repo_full_name == "eandualem/agent-backbone"
