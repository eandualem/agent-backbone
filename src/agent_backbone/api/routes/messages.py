"""Messaging endpoint — deliver a message to an agent via safe_deliver.

Used by agents (agent-to-agent), scripts and dashboards. The message is
wrapped in a provenance envelope so the receiving agent knows who sent it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from agent_backbone.api.deps import get_config, get_db, registered_agent_or_404
from agent_backbone.api.models import MessageRequest, MessageResponse
from agent_backbone.services.routing._delivery import outcome_queues, safe_deliver

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
        swarm = await db.get_swarm(target)
        if swarm is not None and swarm.get("status") == "active":
            target = swarm["coordinator"]
    # Only registered agents are typed into — never an arbitrary tmux session.
    registered_agent_or_404(config, target)

    outcome = await safe_deliver(
        session_name=target,
        message=envelope,
        config=config,
        db=db,
        flow_name="api-messages",
        priority=body.priority,
        delivery_kind="direct_message",
    )

    log.info("Message from %s → %s: %s", body.from_entity, target, outcome)
    return MessageResponse(
        ok=outcome == "delivered",
        session=target,
        outcome=outcome,
        queued=outcome_queues(outcome, "direct_message"),
    )
