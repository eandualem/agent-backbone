"""Messaging endpoint — deliver a message to an agent via safe_deliver.

Used by agents (agent-to-agent), scripts and dashboards. The message is
wrapped in a provenance envelope so the receiving agent knows who sent it.
"""

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
    """Send a message to an agent session using the state-aware delivery pipeline."""
    envelope = f"[via:backbone from:{body.from_entity}] {body.message}"

    outcome = await safe_deliver(
        session_name=body.target_session,
        message=envelope,
        config=config,
        db=db,
        flow_name="api-messages",
        priority=body.priority,
        delivery_kind="direct_message",
    )

    log.info("Message from %s → %s: %s", body.from_entity, body.target_session, outcome)
    return MessageResponse(ok=outcome == "delivered", session=body.target_session, outcome=outcome)
