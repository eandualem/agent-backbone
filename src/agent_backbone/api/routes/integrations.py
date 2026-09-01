"""Integration endpoints — an agent answers the humans on whatever channel they use."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, get_integrations, registered_agent_or_404
from agent_backbone.api.models import IntegrationReplyRequest, IntegrationReplyResponse
from agent_backbone.config import BackboneConfig
from agent_backbone.services.integrations import Integrations

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integrations"])


@router.post("/integrations/reply", response_model=IntegrationReplyResponse)
async def reply_to_humans(
    body: IntegrationReplyRequest,
    config: BackboneConfig = Depends(get_config),
    integrations: Integrations | None = Depends(get_integrations),
):
    """Post an agent's reply into its surface on every enabled integration.

    Telegram: the forum topic mapped to the agent. 503 when no integration
    is configured at all; 404 when none of them has a surface for this
    agent (e.g. no topic yet); 502 when a surface exists but every post
    failed; otherwise the per-integration result.
    """
    if not body.session or not body.text.strip():
        raise HTTPException(status_code=400, detail="session and text required")
    registered_agent_or_404(config, body.session)
    if integrations is None or not integrations.enabled:
        raise HTTPException(status_code=503, detail="no integration is configured")
    results = await integrations.reply_to_agent(body.session, body.text)
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
