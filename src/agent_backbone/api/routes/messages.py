"""Inter-agent messaging endpoint — direct message delivery via deliver_message."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from agent_backbone.api.activity_events import record_activity_event
from agent_backbone.api.deps import get_config, get_db
from agent_backbone.api.models import MessageRequest, MessageResponse
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.messaging import deliver_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["messages"])


@router.post("/messages", response_model=MessageResponse)
async def send_message(
    body: MessageRequest,
    config=Depends(get_config),
    db: BackboneDB = Depends(get_db),
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

    await record_activity_event(
        db,
        session=body.target_session,
        event_type="message.direct_sent",
        entity=body.from_entity,
        data={
            "from_session": body.from_entity,
            "to_session": body.target_session,
            "outcome": outcome,
        },
    )
    if delivered:
        await record_activity_event(
            db,
            session=body.target_session,
            event_type="message.direct_delivered",
            entity="backbone",
            data={
                "from_session": body.from_entity,
                "to_session": body.target_session,
            },
        )
    elif outcome in (
        "offline",
        "agent_working",
        "plan_waiting",
        "permission_waiting",
        "user_interacting",
        "grace_period",
    ):
        await record_activity_event(
            db,
            session=body.target_session,
            event_type="message.direct_queued",
            entity="backbone",
            data={
                "from_session": body.from_entity,
                "to_session": body.target_session,
                "reason": outcome,
            },
        )

    return MessageResponse(ok=delivered, session=body.target_session, outcome=outcome)
