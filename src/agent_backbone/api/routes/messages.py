"""Messaging endpoint — deliver a message to an agent via safe_deliver.

Used by agents (agent-to-agent), scripts and dashboards. The message is
wrapped in a provenance envelope so the receiving agent knows who sent it.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent_backbone.api.deps import get_config, get_db, registered_agent_or_404
from agent_backbone.api.models import MessageRequest, MessageResponse
from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.routing import checkpoint_inbox, queue_detail, safe_deliver

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

    # A swarm is addressed through its coordinator: telling the swarm's name
    # delivers to the coordinator session.
    target = body.target_session
    if config.agents.get(target) is None:
        swarm = await db.swarms.get(target)
        if swarm is not None and swarm.get("status") == "active":
            target = swarm["coordinator"]
    # Only registered agents are typed into — never an arbitrary tmux session.
    registered_agent_or_404(config, target)

    report = await safe_deliver(
        session_name=target,
        message=envelope,
        config=config,
        db=db,
        source="api-messages",
        priority=body.priority,
        delivery_kind="direct_message",
        sender=body.from_entity,
    )

    log.info(
        "Message from %s → %s: %s (%s)", body.from_entity, target, report.outcome, report.queue
    )
    return MessageResponse(
        ok=report.outcome == DeliveryOutcome.DELIVERED,
        session=target,
        outcome=report.outcome.value,
        queued=report.queued,
        queue=report.queue,
        detail=queue_detail(report, target, config.timing.queue_expiry_minutes),
        operation_id=report.operation_id,
        delivery_id=report.delivery_id,
        queue_id=report.queue_id,
    )


class CheckpointRequest(BaseModel):
    session: str = Field(min_length=1, max_length=100)
    acknowledge: list[Annotated[str, Field(min_length=3, max_length=150)]] = Field(
        default_factory=list, max_length=20
    )


@router.post("/messages/inbox")
async def read_checkpoint_inbox(
    body: CheckpointRequest, config=Depends(get_config), db=Depends(get_db)
):
    """Cooperative tool checkpoint; never types into a terminal.

    Session identity follows the existing shared-key/self-asserted sender model.
    Holding a message prevents automatic redelivery; acknowledge after applying
    or explicitly superseding it, and inspect uncertain sends before repeating work.
    """
    registered_agent_or_404(config, body.session)
    try:
        result = await checkpoint_inbox(body.session, db=db, acknowledge=body.acknowledge)
        if body.acknowledge:
            return result
        rows = result["messages"]
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "session": body.session,
        "messages": [
            {
                key: row[key]
                for key in (
                    "id",
                    "operation_id",
                    "ack_token",
                    "message",
                    "sender",
                    "status",
                    "enqueued_at",
                )
            }
            for row in rows
        ],
    }
