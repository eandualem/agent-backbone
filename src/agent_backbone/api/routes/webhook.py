"""Webhook intake routes — migrated from gateway/server.py.

Handles GitHub webhook events and Telegram reply routing.
Pure functions (verify_signature, normalize_event) live in api.webhook_utils.
Dedup uses the persistence service's hot cache via app.state.db.
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
    get_delivery_service,
    get_dispatch_service,
    get_github,
)
from agent_backbone.api.webhook_utils import normalize_event, verify_signature
from agent_backbone.config import BackboneConfig
from agent_backbone.models import EventType
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.routing import DeliveryService, DispatchService
from agent_backbone.services.routing._targets import resolve_event_targets
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


async def dispatch_event_async(
    event,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient,
    delivery_svc: DeliveryService,
    dispatch_svc: DispatchService,
) -> str:
    """Async version of dispatch_event — awaits Prefect flows directly."""
    if event.delivery_id:
        try:
            await db.record_delivery_id(event.delivery_id)
        except Exception:
            log.warning("Failed to persist delivery ID to SQLite")

    if event.event_type == EventType.ISSUE_CLOSED:
        result = await dispatch_svc.on_issue_closed(event, config, gh, db)
        return f"lifecycle: {result}"

    # Skip comments on closed issues — they cannot be actionable and would
    # loop in the queue if delivery is deferred (see #780).
    if event.event_type == EventType.COMMENT_CREATED and event.issue.state == "closed":
        log.info(
            "Ignoring comment on closed issue #%d",
            event.issue.number,
        )
        return f"ignored: comment on closed issue #{event.issue.number}"

    if event.event_type in (
        EventType.ISSUE_OPENED,
        EventType.ISSUE_LABELED,
        EventType.COMMENT_CREATED,
        EventType.PULL_REQUEST_OPENED,
    ):
        if event.event_type in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
            targets = resolve_event_targets(event, config)
            if targets and all(
                delivery_svc.is_recent_notification(
                    event.issue.number,
                    t,
                    repo_full_name=event.issue.repo_full_name,
                )
                for t in targets
            ):
                log.info(
                    "Dedup: #%d reason=all_targets_recently_notified targets=%s",
                    event.issue.number,
                    targets,
                )
                return f"deduped: all targets already notified for #{event.issue.number}"

        result = await dispatch_svc.issue_dispatcher(event, config, db, gh)
        return (
            f"dispatch: {len(result.delivered)} delivered, "
            f"{len(result.offline)} offline, "
            f"{len(result.deferred)} deferred"
        )

    return f"ignored: {event.event_type}"


@router.post("/")
async def handle_webhook(
    request: Request,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    gh: GitHubClient = Depends(get_github),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
    dispatch_svc: DispatchService = Depends(get_dispatch_service),
):
    """Receive GitHub webhook events, validate, dedup, and dispatch."""
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

    outcome = await dispatch_event_async(event, config, db, gh, delivery_svc, dispatch_svc)
    return Response(content=outcome, status_code=200)


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
