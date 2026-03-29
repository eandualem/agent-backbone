"""Inter-agent messaging endpoint — direct message delivery via deliver_message."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from agent_backbone.api.deps import get_config
from agent_backbone.api.models import MessageRequest, MessageResponse
from agent_backbone.services.messaging import deliver_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["messages"])


@router.post("/messages", response_model=MessageResponse)
async def send_message(
    body: MessageRequest,
    config=Depends(get_config),
):
    """Send a coordination message to an agent session via deliver_message.

    Formats the message with a ``[via:backbone from:{from_entity}]`` envelope
    and delivers directly to the target tmux session.
    """
    envelope = f"[via:backbone from:{body.from_entity}] {body.message}"

    outcome = await deliver_message(
        body.target_session,
        envelope,
        config,
        priority=body.priority,
    )

    delivered = outcome == "delivered"
    log.info(
        "Message from %s → %s: %s",
        body.from_entity,
        body.target_session,
        outcome,
    )

    from agent_backbone.api.governance_events import emit_run_event

    await emit_run_event(
        "message.direct_sent",
        context={"from_session": body.from_entity, "to_session": body.target_session},
        source=body.from_entity,
        data={"outcome": outcome},
    )
    if delivered:
        await emit_run_event(
            "message.direct_delivered",
            context={"from_session": body.from_entity, "to_session": body.target_session},
            source="backbone",
        )
    elif outcome in ("offline", "agent_working", "plan_waiting", "permission_waiting", "user_interacting", "grace_period"):
        await emit_run_event(
            "message.direct_queued",
            context={"from_session": body.from_entity, "to_session": body.target_session},
            source="backbone",
            data={"reason": outcome},
        )

    return MessageResponse(ok=delivered, session=body.target_session, outcome=outcome)
