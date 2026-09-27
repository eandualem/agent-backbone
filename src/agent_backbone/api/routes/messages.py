"""Messaging endpoint — deliver a message to an agent via safe_deliver.

Used by agents (agent-to-agent), scripts and dashboards. The message is
wrapped in a provenance envelope so the receiving agent knows who sent it.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_backbone.api import confirmations, validation
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
        try:
            return await _confirmed_message(body, target, signed, request, config, db)
        except HTTPException as exc:
            await confirmations.audit_refusal(
                db, exc, sender=body.from_entity, path="/api/messages", target=target
            )
            raise

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
    outcome, row = await confirmations.admit(
        db, signed, receipt, queued, is_fresh=confirmations.fresh(confirmation, signed)
    )
    await deliver_now(config, db, getattr(request.app.state, "github", None), target)
    row = await db.signing.receipt(confirmation.confirmation_id) or row
    state = await db.queue.by_operation(row["operation_id"])
    status = state["status"] if state else None
    delivered = row["delivered_at"] is not None
    if row["status"] == "revoked":
        result, detail = "revoked", "Its key was reset before delivery; it will not be delivered."
    elif delivered:
        result, detail = "delivered", f"Delivered to {target}."
    elif status == "uncertain":
        result = "uncertain"
        detail = (
            f"Pasted into {target}, but the paste wasn't confirmed; it is held until "
            "the agent acknowledges it (backbone inbox)."
        )
    elif status == "expired" or row.get("outcome") == "expired":
        result, detail = "expired", "It expired before it could be delivered."
    elif status in ("pending", "in_progress", "checkpoint"):
        result = "queued"
        detail = f"Kept for {target}; delivered when the agent is ready."
    else:  # its queued delivery is no longer kept, and nothing says it was delivered
        result = "unknown"
        detail = "Its queued delivery is no longer kept; the receipt shows no delivery."
    if outcome != "admitted":
        detail = f"Already admitted as {confirmation.confirmation_id}. " + detail
    return MessageResponse(
        ok=delivered,
        session=target,
        outcome=result,
        queued=status in ("pending", "in_progress", "checkpoint", "uncertain"),
        queue="stored" if result in ("queued", "uncertain") else None,
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
        try:
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
            _, row = await confirmations.admit(
                db, signed, receipt, None, is_fresh=confirmations.fresh(confirmation, signed)
            )
        except HTTPException as exc:
            await confirmations.audit_refusal(
                db, exc, sender=body.from_entity, path="/api/steer", target=body.target_session
            )
            raise
        token = await db.signing.claim_offer(confirmation.confirmation_id, int(time.time()))
        if token is None:
            # Offered once already, or an earlier request is offering it now:
            # a confirmed steer is never offered twice.
            row = await db.signing.receipt(confirmation.confirmation_id) or row
            state = row.get("offer_state")
            taken = row["delivered_at"] is not None
            if state == "offered" and row.get("outcome") in ("not_taken", "cancelled"):
                outcome, detail = (
                    row["outcome"],
                    "It was offered but never reached the agent; confirm it again.",
                )
            elif state == "offered":
                outcome, detail = (
                    "handed_off" if taken else "offered",
                    f"Already admitted as {confirmation.confirmation_id}.",
                )
            elif state == "failed":
                outcome, detail = (
                    "failed",
                    "An earlier attempt never reached the agent; confirm it again.",
                )
            else:
                outcome, detail = "offering", "An earlier request is offering it now."
            return SteerResponse(
                ok=outcome in ("offered", "handed_off"),
                session=body.target_session,
                outcome=outcome,
                operation_id=row["operation_id"],
                detail=detail,
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
            claim_token=token,
        )
        if report.outcome == "offered":
            await db.signing.mark_offered(confirmation.confirmation_id, token)
        elif report.reason != "claim_lost":
            # Nothing was offered: the confirmation may be sent again.
            await db.signing.discard(confirmation.confirmation_id, token)
            row = None
        else:  # another request holds its offer now; its receipt stays
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
        operation_id=row["operation_id"] if row else report.operation_id,
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


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    done: bool = False


@router.post("/messages/validate")
async def validate_confirmation(
    body: ValidateRequest, request: Request, config=Depends(get_config), db=Depends(get_db)
):
    """``backbone message validate``: claim an owner confirmation for the
    calling agent, identified from its connection, or close the claim."""
    return await validation.validate(request, config, db, body.confirmation_id, body.done)
