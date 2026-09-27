"""Messaging endpoint — deliver a message to an agent via safe_deliver.

Used by agents (agent-to-agent), scripts and dashboards. The message is
wrapped in a provenance envelope so the receiving agent knows who sent it.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent_backbone.api import confirmations
from agent_backbone.api.deps import get_config, get_db, get_feed, registered_agent_or_404
from agent_backbone.api.models import (
    MessageRequest,
    MessageResponse,
    SteerRequest,
    SteerResponse,
)
from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.jobs import deliver_now
from agent_backbone.services.routing import (
    STEER_TTL_SECONDS,
    checkpoint_inbox,
    envelope,
    queue_detail,
    safe_deliver,
    steer_agent,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["messages"])


@router.post("/messages", response_model=MessageResponse)
async def send_message(
    body: MessageRequest,
    background: BackgroundTasks,
    request: Request,
    config=Depends(get_config),
    db=Depends(get_db),
    feed=Depends(get_feed),
):
    """Send a message to an agent session using the state-aware delivery pipeline."""
    # A swarm is addressed through its coordinator: telling the swarm's name
    # delivers to the coordinator session.
    target = body.target_session
    if config.agents.get(target) is None:
        swarm = await db.swarms.get(target)
        if swarm is not None and swarm.get("status") == "active":
            target = swarm["coordinator"]
    # Only registered agents are typed into — never an arbitrary tmux session.
    spec = registered_agent_or_404(config, target)
    signed = getattr(request.state, "signed_sender", None)
    if body.owner_confirmation is not None:
        return await _confirmed_message(body, target, signed, request, config, db)

    report = await safe_deliver(
        session_name=target,
        message=envelope(body.from_entity, body.message, relay=signed is not None),
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
    if report.queue == "stored":
        # After the response: the hint never delays the sender.
        inbox = (target,) if spec.inbox_only else ()
        background.add_task(_hint_inbox, feed, db, target, inbox)
    return MessageResponse(
        ok=report.outcome == DeliveryOutcome.DELIVERED,
        session=target,
        outcome=report.outcome.value,
        queued=report.queued,
        queue=report.queue,
        detail=queue_detail(
            report, target, config.timing.queue_expiry_minutes, inbox_only=spec.inbox_only
        ),
        operation_id=report.operation_id,
        delivery_id=report.delivery_id,
        queue_id=report.queue_id,
    )


async def _confirmed_message(body, target, signed, request, config, db) -> MessageResponse:
    """Admit an owner-confirmed message and try to deliver it at once."""
    confirmation = body.owner_confirmation
    confirmations.check(confirmation, body.message, signed)
    receipt = confirmations.receipt_fields(
        confirmation,
        signed=signed,
        sender=body.from_entity,
        recipient=body.target_session,
        delivered_to=target,
        kind="message",
        text=body.message,
    )
    queued = {
        "session_name": target,
        "message": envelope(
            body.from_entity, body.message, confirmation_id=confirmation.confirmation_id
        ),
        "target_entity": None,
        "source": "api-messages",
        "sender": body.from_entity,
        "priority": int(body.priority),
    }
    outcome, row = await confirmations.admit(db, signed, receipt, queued)
    await deliver_now(config, db, getattr(request.app.state, "github", None), target)
    row = await db.signing.receipt(confirmation.confirmation_id) or row
    state = await db.queue.by_operation(row["operation_id"])
    delivered = row["delivered_at"] is not None
    if row["status"] == "revoked":
        result, detail = "revoked", "Its key was reset before delivery; it will not be delivered."
    elif delivered:
        result, detail = "delivered", f"Delivered to {target}."
    else:
        result = "queued"
        detail = f"Kept for {target}; delivered when the agent is ready."
    if outcome != "admitted":
        detail = f"Already admitted as {confirmation.confirmation_id}. " + detail
    return MessageResponse(
        ok=delivered,
        session=target,
        outcome=result,
        queued=state is not None and state["status"] not in ("delivered", "expired"),
        queue="stored" if result == "queued" else None,
        detail=detail,
        operation_id=row["operation_id"],
        queue_id=state["id"] if state else None,
        confirmation_id=confirmation.confirmation_id,
        receipt=confirmations.public(row),
    )


async def _hint_inbox(feed, db, target: str, inbox: tuple[str, ...]) -> None:
    try:
        await feed.hint_inbox(lambda: db.queue.inbox_rows(target, inbox_sessions=inbox))
    except Exception:
        log.exception("Inbox hint for %s failed (non-fatal)", target)


@router.post("/steer", response_model=SteerResponse)
async def steer(
    body: SteerRequest,
    request: Request,
    config=Depends(get_config),
    db=Depends(get_db),
):
    """Hand a working agent guidance for its current task through its
    runtime's hook — a transient offer, never a queue row and never a paste.
    Refused, with the reason, when the agent is not working, its runtime has
    no hook context (Claude Code, Codex and OpenCode only) or the session was not
    started by the backbone; nothing is queued on refusal."""
    registered_agent_or_404(config, body.target_session)
    signed = getattr(request.state, "signed_sender", None)
    confirmation = body.owner_confirmation
    row = None
    if confirmation is None:
        report = await steer_agent(
            body.target_session,
            body.message,
            config,
            db=db,
            sender=body.from_entity,
            relay=signed is not None,
        )
    else:
        confirmations.check(confirmation, body.message, signed)
        receipt = confirmations.receipt_fields(
            confirmation,
            signed=signed,
            sender=body.from_entity,
            recipient=body.target_session,
            delivered_to=body.target_session,
            kind="steer",
            text=body.message,
        )
        outcome, row = await confirmations.admit(db, signed, receipt, None)
        if outcome != "admitted":  # offered once already: never offer it twice
            row = await db.signing.receipt(confirmation.confirmation_id) or row
            taken = row["delivered_at"] is not None
            return SteerResponse(
                ok=True,
                session=body.target_session,
                outcome="handed_off" if taken else "offered",
                operation_id=row["operation_id"],
                detail=f"Already admitted as {confirmation.confirmation_id}.",
                confirmation_id=confirmation.confirmation_id,
                receipt=confirmations.public(row),
            )
        report = await steer_agent(
            body.target_session,
            body.message,
            config,
            db=db,
            sender=body.from_entity,
            confirmation_id=confirmation.confirmation_id,
            operation_id=row["operation_id"],
        )
        if report.outcome != "offered":
            # Nothing was offered: the confirmation may be sent again.
            await db.signing.discard(confirmation.confirmation_id)
            row = None
    if report.outcome == "offered":
        detail = (
            f"Offered to {report.session}'s current turn; its hook hands it over on the next "
            f"tool call, or it is recorded as not_taken when the turn ends or after "
            f"{STEER_TTL_SECONDS}s "
            f"(delivery {report.delivery_id})."
        )
    elif report.outcome == "refused":
        detail = f"Not offered ({report.reason}); nothing was queued."
    else:
        detail = f"Not offered ({report.reason})."
    return SteerResponse(
        ok=report.outcome == "offered",
        session=report.session,
        outcome=report.outcome,
        reason=report.reason,
        delivery_id=report.delivery_id,
        operation_id=report.operation_id,
        launch_id=report.launch_id,
        evidence=report.evidence,
        detail=detail,
        confirmation_id=confirmation.confirmation_id if row else None,
        receipt=confirmations.public(row) if row else None,
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
    spec = registered_agent_or_404(config, body.session)
    try:
        result = await checkpoint_inbox(
            body.session, db=db, acknowledge=body.acknowledge, escalations=spec.inbox_only
        )
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
