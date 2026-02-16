"""Tests for the FastAPI webhook route at api/routes/webhook.py."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

from gateway.server import _seen_deliveries


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
    async def test_health_returns_ok(self, api_client):
        resp = await api_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestWebhookSignatureValidation:
    def setup_method(self):
        _seen_deliveries.clear()

    async def test_valid_signature_accepted(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes)

        with patch(
            "api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp = await api_client.post("/webhook", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        mock_dispatch.assert_awaited_once()

    async def test_invalid_signature_returns_403(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, signature="sha256=invalid")

        resp = await api_client.post("/webhook", content=payload_bytes, headers=headers)

        assert resp.status_code == 403
        assert resp.text == "Invalid signature"

    async def test_missing_signature_returns_403(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-no-sig",
            "Content-Type": "application/json",
        }

        resp = await api_client.post("/webhook", content=payload_bytes, headers=headers)

        assert resp.status_code == 403
        assert resp.text == "Invalid signature"


class TestWebhookDeduplication:
    def setup_method(self):
        _seen_deliveries.clear()

    async def test_duplicate_delivery_id_returns_200_skipped(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        delivery_id = "dup-delivery-abc"
        headers = _webhook_headers(payload_bytes, delivery_id=delivery_id)

        # First request: dispatches normally
        with patch(
            "api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp1 = await api_client.post("/webhook", content=payload_bytes, headers=headers)
        assert resp1.status_code == 200
        mock_dispatch.assert_awaited_once()

        # Second request with same delivery ID: deduped
        resp2 = await api_client.post("/webhook", content=payload_bytes, headers=headers)
        assert resp2.status_code == 200
        assert resp2.text == "Duplicate, skipped"


class TestWebhookPayloadParsing:
    def setup_method(self):
        _seen_deliveries.clear()

    async def test_invalid_json_returns_400(self, api_client):
        payload_bytes = b"not valid json {{"
        headers = _webhook_headers(payload_bytes, delivery_id="delivery-bad-json")

        resp = await api_client.post("/webhook", content=payload_bytes, headers=headers)

        assert resp.status_code == 400
        assert resp.text == "Invalid JSON"


class TestWebhookDispatch:
    def setup_method(self):
        _seen_deliveries.clear()

    async def test_dispatches_issue_opened_event(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-test-1")

        with patch(
            "api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "dispatch: 1 delivered, 0 offline, 0 deferred"
            resp = await api_client.post("/webhook", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert "dispatch" in resp.text
        # Verify the event was passed to dispatch
        call_args = mock_dispatch.call_args
        event = call_args[0][0]
        assert event.issue.number == 42

    async def test_dispatch_outcome_returned_in_response(self, api_client, webhook_payload):
        payload_bytes = json.dumps(webhook_payload).encode()
        headers = _webhook_headers(payload_bytes, delivery_id="dispatch-test-2")

        with patch(
            "api.routes.webhook.dispatch_event_async", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "ignored: unknown"
            resp = await api_client.post("/webhook", content=payload_bytes, headers=headers)

        assert resp.status_code == 200
        assert resp.text == "ignored: unknown"
