"""Thin HTTP gateway — webhook intake for the automation backbone.

Validates GitHub webhook signatures, deduplicates delivery IDs,
normalizes events, and invokes Prefect flows directly.
Replaces webhook-receiver.py as the webhook endpoint.

Phase II: SQLite-backed dedup loaded on startup for cold-restart recovery.
In-memory OrderedDict kept as hot cache for runtime performance.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler

from src.config import BackboneConfig
from src.dedup import is_recent_notification
from src.models import EventType, IssueEvent
from src.persistence import BackboneDB
from src.topic_discovery import (
    effective_group_chat_id,
    effective_routes,
    load_discovery,
)

log = logging.getLogger(__name__)

# Module-level state
_config: BackboneConfig | None = None
_seen_deliveries: OrderedDict[str, bool] = OrderedDict()


def get_config() -> BackboneConfig:
    global _config
    if _config is None:
        _config = BackboneConfig.from_toml()
    return _config


def is_duplicate(delivery_id: str, max_ids: int = 100) -> bool:
    """Check and record delivery ID for deduplication (in-memory hot cache)."""
    if not delivery_id:
        return False
    if delivery_id in _seen_deliveries:
        return True
    _seen_deliveries[delivery_id] = True
    while len(_seen_deliveries) > max_ids:
        _seen_deliveries.popitem(last=False)
    return False


async def _record_delivery_id_to_db(delivery_id: str, db_path: str) -> None:
    """Record delivery ID to SQLite for cold-restart recovery."""
    if not delivery_id:
        return
    async with BackboneDB(db_path) as db:
        await db.record_delivery_id(delivery_id)


async def _load_dedup_from_db(db_path: str, max_ids: int) -> None:
    """Load recent delivery IDs from SQLite into the hot cache on startup."""
    try:
        async with BackboneDB(db_path) as db:
            cursor = await db.conn.execute(
                "SELECT delivery_id FROM dedup_log ORDER BY received_at DESC LIMIT ?",
                (max_ids,),
            )
            rows = await cursor.fetchall()
            for row in reversed(rows):  # oldest first into OrderedDict
                _seen_deliveries[row["delivery_id"]] = True
        log.info("Loaded %d delivery IDs from SQLite", len(_seen_deliveries))
    except Exception:
        log.warning("Could not load dedup state from SQLite — starting fresh")


def verify_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def normalize_event(
    event_type_str: str, action: str, payload: dict, delivery_id: str
) -> IssueEvent:
    """Normalize a raw GitHub webhook into an IssueEvent."""
    return IssueEvent.from_webhook(event_type_str, action, payload, delivery_id)


def dispatch_event(event: IssueEvent) -> str:
    """Route an event to the appropriate Prefect flow.

    Uses asyncio.run() for Phase 1 direct invocation.
    """
    config = get_config()

    # Persist delivery ID to SQLite for cold-restart recovery
    if event.delivery_id:
        try:
            asyncio.run(_record_delivery_id_to_db(event.delivery_id, str(config.delivery.db_file)))
        except Exception:
            log.warning("Failed to persist delivery ID to SQLite")

    if event.event_type == EventType.ISSUE_CLOSED:
        from flows.lifecycle import on_issue_closed

        result = asyncio.run(on_issue_closed(event))
        return f"lifecycle: {result}"

    if event.event_type in (
        EventType.ISSUE_OPENED,
        EventType.ISSUE_LABELED,
        EventType.COMMENT_CREATED,
    ):
        # Notification-level dedup for ALL dispatch events.
        targets = event.issue.labels.targets
        if targets and all(is_recent_notification(event.issue.number, t) for t in targets):
            log.info(
                "Dedup: #%d reason=all_targets_recently_notified targets=%s",
                event.issue.number,
                targets,
            )
            return f"deduped: all targets already notified for #{event.issue.number}"

        from flows.issue_dispatcher import issue_dispatcher

        result = asyncio.run(issue_dispatcher(event))
        return (
            f"dispatch: {len(result.delivered)} delivered, "
            f"{len(result.offline)} offline, "
            f"{len(result.deferred)} deferred"
        )

    return f"ignored: {event.event_type}"


def _reverse_topic_routes(routes: dict[int, str]) -> dict[str, int]:
    """Build session_name → thread_id mapping from topic_routes."""
    return {
        session: thread_id for thread_id, session in routes.items() if session != "coding-agents"
    }


def send_telegram_reply(token: str, group_chat_id: int, thread_id: int, text: str) -> bool:
    """Send a message to a Telegram topic via the Bot API. Returns True on success."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": group_chat_id,
            "message_thread_id": thread_id,
            "text": text,
        }
    ).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        log.exception("Telegram sendMessage failed")
        return False


class GatewayHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path == "/api/reply":
            return self._handle_reply()

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

        # Dedup check (delivery ID level)
        delivery_id = self.headers.get("X-GitHub-Delivery", "")
        if is_duplicate(delivery_id, config.max_delivery_ids):
            log.info("Dedup: delivery_id=%s reason=duplicate_delivery_id", delivery_id)
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
            "Received: delivery=%s event=%s action=%s #%d targets=%s",
            delivery_id,
            event_type_str,
            action,
            event.issue.number,
            event.issue.labels.targets,
        )

        outcome = dispatch_event(event)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(outcome.encode())

    def _handle_reply(self) -> None:
        """Handle POST /api/reply — route agent replies to Telegram topics."""
        config = get_config()
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        session = data.get("session", "")
        text = data.get("text", "")
        if not session or not text:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"session and text required"}')
            return

        discovery = load_discovery(config.telegram.topic_discovery_path)
        group_chat_id = effective_group_chat_id(config, discovery)
        if not group_chat_id:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"group_chat_id not configured"}')
            return

        merged_routes = effective_routes(config, discovery)
        reverse_map = _reverse_topic_routes(merged_routes)
        thread_id = reverse_map.get(session)
        if not thread_id:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(
                json.dumps({"error": f"no topic route for session '{session}'"}).encode()
            )
            return

        token = os.environ.get("TELEGRAM_TOKEN", "")
        if not token:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"TELEGRAM_TOKEN not set"}')
            return

        ok = send_telegram_reply(token, group_chat_id, thread_id, text)
        if ok:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "session": session}).encode())
        else:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b'{"error":"Telegram API call failed"}')

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
    import uvicorn

    from api.app import create_app

    config = get_config()
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=config.gateway_port, log_level="info")


if __name__ == "__main__":
    main()
