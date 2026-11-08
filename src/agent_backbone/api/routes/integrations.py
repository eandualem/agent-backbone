"""Integration endpoints — an agent answers the humans on whatever channel they use."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_backbone.api.deps import get_config, get_db, get_integrations, registered_agent_or_404
from agent_backbone.api.models import IntegrationReplyRequest, IntegrationReplyResponse
from agent_backbone.config import BackboneConfig
from agent_backbone.services.integrations import Integrations

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integrations"])


@router.post("/integrations/reply", response_model=IntegrationReplyResponse)
async def reply_to_humans(
    body: IntegrationReplyRequest,
    request: Request,
    db=Depends(get_db),
    config: BackboneConfig = Depends(get_config),
    integrations: Integrations | None = Depends(get_integrations),
):
    """Post an agent's reply into its surface on every enabled integration.

    Telegram: the forum topic mapped to the agent. 403 for swarm participants.
    503 when no integration
    is configured at all; 404 when none of them has a surface for this
    agent (e.g. no topic yet); 502 when a surface exists but every post
    failed; otherwise the per-integration result.
    """
    if not body.session or not body.text.strip():
        raise HTTPException(status_code=400, detail="session and text required")
    spec = registered_agent_or_404(config, body.session)
    if any(tag.startswith("swarm:") for tag in spec.tags):
        raise HTTPException(
            status_code=403,
            detail=(
                "Swarm participants must send findings through the coordinator "
                "to the repository agent"
            ),
        )
    if integrations is None or not integrations.enabled:
        raise HTTPException(status_code=503, detail="no integration is configured")
    registration = getattr(request.state, "credential_registration", None)
    if registration and request.state.credential_agent != body.session:
        raise HTTPException(403, "Sender does not match credential")
    operation = uuid.uuid4().hex
    await db.interactions.capture(
        operation_id=operation,
        message=body.text,
        sender=body.session,
        recipient="owner",
        kind="message",
        source="integration-reply",
        metadata={
            "sender": await db.interactions.attribution(body.session, registration),
            "recipient": {
                "kind": "owner",
                "id": "owner",
                "name": "You",
                "name_at_capture": "You",
                "attribution": "system",
                "retired": False,
            },
        },
    )
    results = await integrations.reply_to_agent(body.session, body.text)
    try:
        await db.interactions.observe(
            operation,
            "integration_delivery",
            outcome="delivered" if any(v == "posted" for v in results.values()) else "failed",
            source="integration-reply",
        )
    except Exception:
        # The reply already reached the person: a history write never turns it into a failure.
        log.warning("Integration reply was sent but its history event was not recorded")
    posted = {name: outcome == "posted" for name, outcome in results.items()}
    if not any(posted.values()):
        failed = sorted(name for name, outcome in results.items() if outcome == "failed")
        if failed:
            raise HTTPException(status_code=502, detail=f"posting failed on: {', '.join(failed)}")
        raise HTTPException(
            status_code=404,
            detail=f"no integration has a channel for '{body.session}' yet",
        )
    return IntegrationReplyResponse(ok=True, session=body.session, posted=posted, results=results)
