"""Owner-confirmed messages and steers at the API (docs/owner-confirmed-messages.md).

A confirmation is admitted only on a verified signed request: its hash and
age are checked, then the nonce, the immutable receipt and (for a message)
the queued delivery are committed in one transaction. The owner-confirmed
marker is written only from that receipt.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime

from fastapi import HTTPException

from agent_backbone import signing
from agent_backbone.api.models import OwnerConfirmation
from agent_backbone.api.signed import SignedSender

MAX_AGE_SECONDS = 1800
PUBLIC_FIELDS = (
    "seq",
    "confirmation_id",
    "sender",
    "recipient",
    "kind",
    "text",
    "text_sha256",
    "source",
    "confirmed_at",
    "delivered_at",
    "key_epoch",
)


def refuse(status: int, reason: str, message: str) -> HTTPException:
    return HTTPException(status, {"reason": reason, "message": message})


def check(confirmation: OwnerConfirmation, text: str, signed: SignedSender | None) -> None:
    """Refuse a confirmation that isn't signed, doesn't match the text, or is
    too old; nothing is recorded on a refusal."""
    if signed is None or not signed.nonce_in_route:
        raise refuse(
            403, "not_enrolled", "a confirmation needs a request signed by an enrolled key"
        )
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != confirmation.text_sha256:
        raise refuse(400, "text_hash_mismatch", "text_sha256 does not match the message")
    confirmed = datetime.fromisoformat(confirmation.confirmed_at).timestamp()
    if signed.timestamp - confirmed > MAX_AGE_SECONDS or confirmed - signed.timestamp > (
        signing.SKEW_SECONDS
    ):
        raise refuse(400, "confirmation_expired", "confirmed_at is outside the allowed age")


def receipt_fields(
    confirmation: OwnerConfirmation,
    *,
    signed: SignedSender,
    sender: str,
    recipient: str,
    delivered_to: str,
    kind: str,
    text: str,
) -> dict:
    return {
        "confirmation_id": confirmation.confirmation_id,
        "sender": sender,
        "sender_key": signed.sender_key,
        "recipient": recipient,
        "delivered_to": delivered_to,
        "kind": kind,
        "text": text,
        "text_sha256": confirmation.text_sha256,
        "source": confirmation.source,
        "confirmed_at": confirmation.confirmed_at,
        "key_epoch": signed.epoch,
        "operation_id": uuid.uuid4().hex,
    }


async def admit(db, signed: SignedSender, receipt: dict, queue: dict | None) -> tuple[str, dict]:
    """Commit the confirmation; ``(outcome, receipt row)``. Refuses a reused
    nonce or a confirmation id taken by a different confirmation (409)."""
    outcome, row = await db.signing.admit(
        nonce=signed.nonce,
        request_hash=signed.request_hash,
        now=int(time.time()),
        receipt=receipt,
        queue=queue,
    )
    if outcome == "nonce_reused":
        raise refuse(
            409, "nonce_reused", "this nonce was already used; sign again with a fresh one"
        )
    if outcome == "conflict":
        raise refuse(
            409, "confirmation_conflict", "this confirmation id belongs to a different confirmation"
        )
    return outcome, row


def public(row: dict) -> dict:
    """The receipt as the API returns it."""
    return {field: row[field] for field in PUBLIC_FIELDS}
