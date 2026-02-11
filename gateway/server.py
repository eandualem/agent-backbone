"""Thin HTTP gateway — webhook intake for the automation backbone.

Validates GitHub webhook signatures, deduplicates delivery IDs,
normalizes events, and invokes Prefect flows directly.
Replaces webhook-receiver.py as the webhook endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer

from src.config import BackboneConfig
from src.models import EventType, IssueEvent

log = logging.getLogger(__name__)

# Module-level state
_config: BackboneConfig | None = None
_seen_deliveries: OrderedDict[str, bool] = OrderedDict()
_recent_notifications: dict[tuple[int, str], float] = {}


def get_config() -> BackboneConfig:
    global _config
    if _config is None:
        _config = BackboneConfig()
    return _config


def is_duplicate(delivery_id: str, max_ids: int = 100) -> bool:
    """Check and record delivery ID for deduplication."""
    if not delivery_id:
        return False
    if delivery_id in _seen_deliveries:
        return True
    _seen_deliveries[delivery_id] = True
    while len(_seen_deliveries) > max_ids:
        _seen_deliveries.popitem(last=False)
    return False


def is_recent_notification(issue_number: int, target: str, dedup_seconds: int = 5) -> bool:
    """Suppress duplicate notifications for the same issue+target within a time window."""
    key = (issue_number, target)
    now = time.monotonic()
    expired = [k for k, t in _recent_notifications.items() if now - t > dedup_seconds]
    for k in expired:
        del _recent_notifications[k]
    if key in _recent_notifications:
        return True
    _recent_notifications[key] = now
    return False


def verify_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def normalize_event(event_type_str: str, action: str, payload: dict, delivery_id: str) -> IssueEvent:
    """Normalize a raw GitHub webhook into an IssueEvent."""
    return IssueEvent.from_webhook(event_type_str, action, payload, delivery_id)


def dispatch_event(event: IssueEvent) -> str:
    """Route an event to the appropriate Prefect flow.

    Uses asyncio.run() for Phase 1 direct invocation.
    """
    if event.event_type == EventType.ISSUE_CLOSED:
        from flows.lifecycle import on_issue_closed

        result = asyncio.run(on_issue_closed(event))
        return f"lifecycle: {result}"

    if event.event_type in (
        EventType.ISSUE_OPENED,
        EventType.ISSUE_LABELED,
        EventType.COMMENT_CREATED,
    ):
        # Check notification-level dedup for issue events
        if event.event_type in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
            for target in event.issue.labels.targets:
                if is_recent_notification(event.issue.number, target):
                    log.info(
                        "Suppressed duplicate notification for #%d → %s",
                        event.issue.number,
                        target,
                    )

        from flows.issue_dispatcher import issue_dispatcher

        result = asyncio.run(issue_dispatcher(event))
        return f"dispatch: {len(result.delivered)} delivered, {len(result.offline)} offline"

    return f"ignored: {event.event_type}"


class GatewayHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        config = get_config()
        content_length = int(self.headers.get("Content-Length", 0))
        payload_body = self.rfile.read(content_length)

        # Verify signature
        signature = self.headers.get("X-Hub-Signature-256")
        if config.webhook_secret and not verify_signature(
            payload_body, signature, config.webhook_secret
        ):
            log.warning("Invalid webhook signature — rejecting")
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Invalid signature")
            return

        # Dedup check
        delivery_id = self.headers.get("X-GitHub-Delivery", "")
        if is_duplicate(delivery_id, config.max_delivery_ids):
            log.info("Duplicate delivery %s — skipping", delivery_id)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Duplicate, skipped")
            return

        # Parse payload
        try:
            payload = json.loads(payload_body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        event_type_str = self.headers.get("X-GitHub-Event", "")
        action = payload.get("action", "")

        # Normalize and dispatch
        event = normalize_event(event_type_str, action, payload, delivery_id)
        log.info(
            "Event: %s #%d (targets: %s)",
            event.event_type.value,
            event.issue.number,
            event.issue.labels.targets,
        )

        outcome = dispatch_event(event)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(outcome.encode())

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        log.info(format, *args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    config = get_config()
    server = HTTPServer(("127.0.0.1", config.gateway_port), GatewayHandler)
    log.info("Gateway listening on http://127.0.0.1:%d/webhook", config.gateway_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
