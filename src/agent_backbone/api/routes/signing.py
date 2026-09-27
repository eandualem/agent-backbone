"""Signed senders: enrollment, rotation and what an app reads about them.

See docs/owner-confirmed-messages.md. A transition only waits here; it takes
effect when the owner sends ``/approve_key <digest>`` in Telegram, with the
digest copied from the app — never from a Backbone message.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError

from agent_backbone import signing
from agent_backbone.api.deps import get_config, get_db
from agent_backbone.api.models import envelope_sender
from agent_backbone.api.signed import Refusal, now
from agent_backbone.services.integrations import notify_humans

router = APIRouter(prefix="/api/signing", tags=["signing"])

TRANSITION_SECONDS = 900
DEFAULT_SENDERS = frozenset({"api", "unknown", "backbone"})
"""Names the API fills in when a request gives none (who approved or denied a
prompt, a skill change's actor, a restart's continuation); enrolling one would
leave an unsigned path to it. A swarm's missing initiator is recorded as
'(human operator)', which no signing name can be."""


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender: str
    action: Literal["set", "replace", "clear"]
    expected_epoch: int = Field(ge=0)
    new_public_key: str | None
    request_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    proof: str | None

    @field_validator("sender")
    @classmethod
    def _header_safe(cls, value: str) -> str:
        """A signing name travels in a header, so it is printable ASCII."""
        envelope_sender(value)
        if not value.isascii() or not value.isprintable():
            raise ValueError("a signing sender name is printable ASCII")
        if signing.same_name(value) in DEFAULT_SENDERS:
            raise ValueError(f"'{value}' is what Backbone records when no sender is given")
        return value


class RotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_public_key: str
    proof: str


class OwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram_user_id: int = Field(gt=0)


def _refuse(status: int, reason: str, message: str) -> HTTPException:
    return HTTPException(status, {"reason": reason, "message": message})


def _view(transition: dict) -> dict:
    return {
        "action": transition["action"],
        "sender": transition["sender"],
        "audience": transition["audience"],
        "expected_epoch": transition["expected_epoch"],
        "new_epoch": transition["new_epoch"],
        "new_fingerprint": transition["new_fingerprint"],
        "request_id": transition["request_id"],
        "expires_at": transition["expires_at"],
    }


@router.get("/audience")
async def audience(db=Depends(get_db)):
    """This install's audience id; an app pins it when it enrolls."""
    return {"audience": await db.signing.audience()}


@router.get("/enrollment")
async def enrollment(sender: str = Query(...), db=Depends(get_db)):
    """A name's active key and epoch, and its pending transition, if any."""
    key = signing.same_name(sender)
    enrolled = await db.signing.enrollment(key)
    pending = await db.signing.pending(key, now())
    return {
        "sender": enrolled["sender"] if enrolled else sender,
        "audience": await db.signing.audience(),
        "epoch": enrolled["epoch"] if enrolled else 0,
        "fingerprint": enrolled["fingerprint"] if enrolled else None,
        "pending": _view(pending) if pending else None,
    }


@router.post("/transitions")
async def start_transition(body: TransitionRequest, config=Depends(get_config), db=Depends(get_db)):
    """Store a set, replace or clear for the owner to approve in Telegram."""
    key = signing.same_name(body.sender)
    enrolled = await db.signing.enrollment(key)
    current = enrolled["epoch"] if enrolled else 0
    if body.action == "set" and enrolled is not None:
        raise _refuse(409, "already_enrolled", "the name has a key; use replace or clear")
    if body.action != "set" and enrolled is None:
        raise _refuse(409, "not_enrolled", "the name has no key to replace or clear")
    if body.expected_epoch != current:
        raise _refuse(409, "epoch_changed", f"the current epoch is {current}")
    audience_id = await db.signing.audience()
    if body.action == "clear":
        if body.new_public_key is not None or body.proof is not None:
            raise _refuse(422, "malformed_request", "a clear names no new key")
        fingerprint = "none"
    else:
        if body.new_public_key is None or body.proof is None:
            raise _refuse(422, "malformed_request", "a set or replace needs a key and a proof")
        try:
            raw = signing.public_key(body.new_public_key)
            proof = signing.b64url_decode(body.proof)
        except ValueError as exc:
            raise _refuse(422, "malformed_request", str(exc)) from exc
        framed = signing.enroll_proof_bytes(
            sender=body.sender,
            audience=audience_id,
            action=body.action,
            expected_epoch=body.expected_epoch,
            new_public_key=body.new_public_key,
            request_id=body.request_id,
        )
        try:
            valid = signing.verify(raw, proof, framed)
        except signing.VerifierUnavailable as exc:
            raise _refuse(503, "verifier_unavailable", "signatures can't be checked") from exc
        if not valid:
            raise _refuse(403, "signature_invalid", "the proof does not verify with the new key")
        fingerprint = signing.fingerprint(raw)
    expires_at = now() + TRANSITION_SECONDS
    digest = signing.transition_digest(
        action=body.action,
        sender=body.sender,
        audience=audience_id,
        expected_epoch=body.expected_epoch,
        new_fingerprint=fingerprint,
        request_id=body.request_id,
        expires_at=expires_at,
    )
    try:
        transition = await db.signing.start_transition(
            sender=body.sender,
            sender_key=key,
            action=body.action,
            audience=audience_id,
            expected_epoch=body.expected_epoch,
            new_public_key=body.new_public_key,
            new_fingerprint=fingerprint,
            request_id=body.request_id,
            digest=digest,
            expires_at=expires_at,
        )
    except IntegrityError as exc:  # the request id is unique
        raise _refuse(409, "request_id_reused", "this request id was already used") from exc
    await notify_humans(
        config,
        f"A key change for '{body.sender}' is waiting: {body.action}, new key "
        f"{signing.grouped(fingerprint)[:35]}. If you started it, send /approve_key with "
        "the digest the app shows you. Otherwise ignore it; it expires in 15 minutes.",
    )
    return _view(transition)


@router.post("/rotation")
async def rotate(
    body: RotationRequest, request: Request, config=Depends(get_config), db=Depends(get_db)
):
    """Replace the key, signed by the current one (checked before this route)."""
    signed = getattr(request.state, "signed_sender", None)
    if signed is None:  # the middleware refuses first; this is a second lock
        raise Refusal(403, "signature_required", "a signed request is required").as_http()
    try:
        raw = signing.public_key(body.new_public_key)
        proof = signing.b64url_decode(body.proof)
    except ValueError as exc:
        raise _refuse(422, "malformed_request", str(exc)) from exc
    framed = signing.rotate_proof_bytes(
        sender=signed.sender,
        audience=await db.signing.audience(),
        old_epoch=signed.epoch,
        new_public_key=body.new_public_key,
        nonce=signed.nonce,
        timestamp=signed.timestamp,
    )
    try:
        valid = signing.verify(raw, proof, framed)
    except signing.VerifierUnavailable as exc:
        raise _refuse(503, "verifier_unavailable", "signatures can't be checked") from exc
    if not valid:
        raise _refuse(403, "signature_invalid", "the proof does not verify with the new key")
    rotated = await db.signing.rotate(
        sender_key=signed.sender_key,
        old_epoch=signed.epoch,
        new_public_key=body.new_public_key,
        new_fingerprint=signing.fingerprint(raw),
    )
    if rotated is None:
        raise _refuse(409, "epoch_changed", "the key changed while this request was checked")
    await notify_humans(
        config,
        f"The key for '{rotated['sender']}' was rotated by the app (epoch {rotated['epoch']}, "
        f"new key {signing.grouped(rotated['fingerprint'])[:35]}).",
    )
    return {"epoch": rotated["epoch"], "fingerprint": rotated["fingerprint"]}


@router.get("/observations")
async def observations(
    sender: str = Query(...),
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db=Depends(get_db),
):
    """How requests made as the name fared against its pending key."""
    rows = await db.signing.observations(signing.same_name(sender), after, limit)
    return {"observations": rows, "next_after": rows[-1]["seq"] if rows else after}


@router.post("/owner")
async def set_owner(body: OwnerRequest, config=Depends(get_config), db=Depends(get_db)):
    """Set the owner's Telegram id once; a later change waits for the current owner."""
    outcome = await db.signing.set_owner(body.telegram_user_id)
    if outcome == "set":
        await notify_humans(
            config, f"Key approvals now come from Telegram user {body.telegram_user_id}."
        )
    else:
        await notify_humans(
            config,
            f"A change of the approving Telegram user to {body.telegram_user_id} was requested. "
            f"If that's you, the current owner sends /approve_owner {body.telegram_user_id}.",
        )
    return {"outcome": outcome}
