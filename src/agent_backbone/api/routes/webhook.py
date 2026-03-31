"""Webhook intake routes.

Validates GitHub webhook signatures, deduplicates by delivery ID, normalizes
events, and hands off to IssueService. Also routes Telegram replies.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from fastapi import APIRouter, Depends, Request, Response

from agent_backbone.api.auth import require_api_key
from agent_backbone.api.deps import (
    get_config,
    get_db,
    get_issue_service,
)
from agent_backbone.api.webhook_utils import normalize_event, verify_signature
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.issues import IssueService
from agent_backbone.services.telegram._topic_discovery import (
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


@router.post("/")
async def handle_webhook(
    request: Request,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    issue_service: IssueService = Depends(get_issue_service),
):
    """Receive GitHub webhook events — validate, dedup, normalize, hand off."""
    payload_body = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    if config.webhook_secrets and not any(
        verify_signature(payload_body, signature, secret) for secret in config.webhook_secrets
    ):
        log.warning("Invalid webhook signature — rejecting")
        return Response(content="Invalid signature", status_code=403)

    # Dedup check (delivery ID level) — uses persistence hot cache
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if db.is_duplicate(delivery_id, config.gateway.max_delivery_ids):
        log.info("Dedup: delivery_id=%s reason=duplicate_delivery_id", delivery_id)
        return Response(content="Duplicate, skipped", status_code=200)

    # Parse payload
    try:
        payload = json.loads(payload_body)
    except json.JSONDecodeError:
        return Response(content="Invalid JSON", status_code=400)

    event_type_str = request.headers.get("X-GitHub-Event", "")
    action = payload.get("action", "")

    event = normalize_event(event_type_str, action, payload, delivery_id)
    log.info(
        "Received: delivery=%s event=%s action=%s #%d repo=%s",
        delivery_id,
        event_type_str,
        action,
        event.issue.number,
        event.issue.repo_full_name,
    )

    await issue_service.handle_webhook_event(event)

    return Response(content="accepted", status_code=200)


@router.post("/api/reply", dependencies=[Depends(require_api_key)])
async def handle_reply(
    request: Request,
    config: BackboneConfig = Depends(get_config),
):
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
