"""Webhook intake routes — migrated from gateway/server.py.

Handles GitHub webhook events and Telegram reply routing.
Pure functions (verify_signature, normalize_event, is_duplicate) remain
in gateway/server.py and are imported here.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from fastapi import APIRouter, Depends, Request, Response

from api.deps import get_config
from gateway.server import (
    _record_delivery_id_to_db,
    is_duplicate,
    normalize_event,
    verify_signature,
)
from src.config import BackboneConfig
from src.dedup import is_recent_notification
from src.models import EventType
from src.topic_discovery import (
    effective_group_chat_id,
    effective_routes,
    load_discovery,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _reverse_topic_routes(routes: dict[int, str]) -> dict[str, int]:
    """Build session_name → thread_id mapping from topic_routes."""
    return {
        session: thread_id for thread_id, session in routes.items() if session != "coding-agents"
    }


async def dispatch_event_async(event, config: BackboneConfig) -> str:
    """Async version of dispatch_event — awaits Prefect flows directly."""
    if event.delivery_id:
        try:
            await _record_delivery_id_to_db(event.delivery_id, str(config.delivery.db_file))
        except Exception:
            log.warning("Failed to persist delivery ID to SQLite")

    if event.event_type == EventType.ISSUE_CLOSED:
        from flows.lifecycle import on_issue_closed

        result = await on_issue_closed(event)
        return f"lifecycle: {result}"

    if event.event_type in (
        EventType.ISSUE_OPENED,
        EventType.ISSUE_LABELED,
        EventType.COMMENT_CREATED,
    ):
        targets = event.issue.labels.targets
        if targets and all(is_recent_notification(event.issue.number, t) for t in targets):
            log.info(
                "Dedup: #%d reason=all_targets_recently_notified targets=%s",
                event.issue.number,
                targets,
            )
            return f"deduped: all targets already notified for #{event.issue.number}"

        from flows.issue_dispatcher import issue_dispatcher

        result = await issue_dispatcher(event)
        return (
            f"dispatch: {len(result.delivered)} delivered, "
            f"{len(result.offline)} offline, "
            f"{len(result.deferred)} deferred"
        )

    return f"ignored: {event.event_type}"


@router.post("/webhook")
async def handle_webhook(request: Request, config: BackboneConfig = Depends(get_config)):
    """Receive GitHub webhook events, validate, dedup, and dispatch."""
    payload_body = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    if config.webhook_secret and not verify_signature(
        payload_body, signature, config.webhook_secret
    ):
        log.warning("Invalid webhook signature — rejecting")
        return Response(content="Invalid signature", status_code=403)

    # Dedup check (delivery ID level)
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if is_duplicate(delivery_id, config.max_delivery_ids):
        log.info("Dedup: delivery_id=%s reason=duplicate_delivery_id", delivery_id)
        return Response(content="Duplicate, skipped", status_code=200)

    # Parse payload
    try:
        payload = json.loads(payload_body)
    except json.JSONDecodeError:
        return Response(content="Invalid JSON", status_code=400)

    event_type_str = request.headers.get("X-GitHub-Event", "")
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

    outcome = await dispatch_event_async(event, config)
    return Response(content=outcome, status_code=200)


@router.post("/api/reply")
async def handle_reply(request: Request, config: BackboneConfig = Depends(get_config)):
    """Route agent replies to Telegram topics."""
    body = await request.body()

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return Response(content="Invalid JSON", status_code=400)

    session = data.get("session", "")
    text = data.get("text", "")
    if not session or not text:
        return Response(
            content=json.dumps({"error": "session and text required"}),
            status_code=400,
            media_type="application/json",
        )

    discovery = load_discovery(config.telegram.topic_discovery_path)
    group_chat_id = effective_group_chat_id(config, discovery)
    if not group_chat_id:
        return Response(
            content=json.dumps({"error": "group_chat_id not configured"}),
            status_code=500,
            media_type="application/json",
        )

    merged_routes = effective_routes(config, discovery)
    reverse_map = _reverse_topic_routes(merged_routes)
    thread_id = reverse_map.get(session)
    if not thread_id:
        return Response(
            content=json.dumps({"error": f"no topic route for session '{session}'"}),
            status_code=404,
            media_type="application/json",
        )

    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        return Response(
            content=json.dumps({"error": "TELEGRAM_TOKEN not set"}),
            status_code=500,
            media_type="application/json",
        )

    ok = await _send_telegram_reply_async(token, group_chat_id, thread_id, text)
    if ok:
        return {"ok": True, "session": session}
    return Response(
        content=json.dumps({"error": "Telegram API call failed"}),
        status_code=502,
        media_type="application/json",
    )


async def _send_telegram_reply_async(
    token: str, group_chat_id: int, thread_id: int, text: str
) -> bool:
    """Send a message to a Telegram topic via httpx (async replacement for urllib)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": group_chat_id,
        "message_thread_id": thread_id,
        "text": text,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            return resp.status_code == 200
    except Exception:
        log.exception("Telegram sendMessage failed")
        return False
