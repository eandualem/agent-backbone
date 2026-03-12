"""Inter-agent messaging endpoint — direct message delivery via safe_deliver."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from agent_backbone.api.deps import get_config, get_db
from agent_backbone.api.models import MessageRequest, MessageResponse
from agent_backbone.services.routing._delivery import safe_deliver

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["messages"])


@router.post("/messages", response_model=MessageResponse)
async def send_message(
    body: MessageRequest,
    config=Depends(get_config),
    db=Depends(get_db),
):
    """Send a coordination message to an agent session via safe_deliver.

    Formats the message with a ``[via:backbone from:{from_entity}]`` envelope
    and delivers using the state-aware safe_deliver pipeline.
    """
    envelope = f"[via:backbone from:{body.from_entity}] {body.message}"

    outcome = await safe_deliver(
        session_name=body.target_session,
        message=envelope,
        config=config,
        db=db,
        priority=body.priority,
    )

    delivered = outcome == "delivered"
    log.info(
        "Message from %s → %s: %s",
        body.from_entity,
        body.target_session,
        outcome,
    )

    return MessageResponse(ok=delivered, session=body.target_session, outcome=outcome)
