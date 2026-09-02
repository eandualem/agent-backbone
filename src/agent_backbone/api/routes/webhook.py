"""GitHub webhook intake.

``POST /webhooks/github`` receives GitHub webhook events, verifies the HMAC
signature, deduplicates by delivery id, and dispatches to the routing layer.
Configure the webhook for **Issues**, **Issue comments** and optionally
**Pull requests**.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Request, Response

from agent_backbone.api.deps import (
    get_config,
    get_db,
    get_delivery_service,
    get_dispatch_service,
    get_optional_github,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.models import IssueEvent
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.routing import DeliveryService, DispatchService, dispatch_event

log = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])


def verify_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def _handle(
    request: Request,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient | None,
    delivery_svc: DeliveryService,
    dispatch_svc: DispatchService,
) -> Response:
    payload_body = await request.body()

    if not config.webhook_secret:
        log.warning("Webhook received but GITHUB_WEBHOOK_SECRET is not set — rejecting")
        return Response(content="Webhook secret not configured", status_code=403)

    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(payload_body, signature, config.webhook_secret):
        log.warning("Invalid webhook signature — rejecting")
        return Response(content="Invalid signature", status_code=403)

    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if db.is_duplicate(delivery_id, config.backbone.max_delivery_ids):
        log.info("Dedup: delivery_id=%s reason=duplicate_delivery_id", delivery_id)
        return Response(content="Duplicate, skipped", status_code=200)

    try:
        payload = json.loads(payload_body)
    except json.JSONDecodeError:
        db.forget_delivery(delivery_id)
        return Response(content="Invalid JSON", status_code=400)

    event_type_str = request.headers.get("X-GitHub-Event", "")
    if event_type_str == "ping":
        return Response(content="pong", status_code=200)
    action = payload.get("action", "")

    # If anything past this point fails, drop the delivery id from the hot
    # cache so GitHub's retry of the same delivery is processed, not skipped.
    try:
        event = IssueEvent.from_webhook(event_type_str, action, payload, delivery_id)
        log.info(
            "Received: delivery=%s event=%s action=%s #%d targets=%s",
            delivery_id,
            event_type_str,
            action,
            event.issue.number,
            event.issue.labels.targets,
        )
        outcome = await dispatch_event(event, config, db, gh, delivery_svc, dispatch_svc)
    except Exception:
        db.forget_delivery(delivery_id)
        raise
    return Response(content=outcome, status_code=200)


@router.post("/webhooks/github")
async def handle_webhook(
    request: Request,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    gh: GitHubClient | None = Depends(get_optional_github),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
    dispatch_svc: DispatchService = Depends(get_dispatch_service),
):
    """Receive GitHub webhook events, validate, dedup, and dispatch."""
    return await _handle(request, config, db, gh, delivery_svc, dispatch_svc)
