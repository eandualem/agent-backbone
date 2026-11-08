"""Resolve explicit sender attribution before capturing a normal send."""

from fastapi import HTTPException


async def signed_as(request, db, name: str) -> bool:
    """Whether this request was signed by the key enrolled under exactly ``name``.

    The middleware matches names case-insensitively, and case-twin registrations can
    exist: only the name recorded at enrollment may receive signed attribution."""
    signed = getattr(request.state, "signed_sender", None)
    if signed is None:
        return False
    enrolled = await db.signing.enrollment(signed.sender_key)
    return enrolled is not None and enrolled["sender"] == name


async def metadata(request, body, db):
    supplied = body.interaction
    operator = request.state.credential_role == "operator"
    owner_origin = supplied is not None and supplied.actor == "owner"
    if owner_origin and not operator:
        raise HTTPException(403, "Owner interaction attribution requires operator authority")
    registration = getattr(request.state, "credential_registration", None)
    name = getattr(request.state, "credential_agent", None)
    if registration and name != body.from_entity:
        raise HTTPException(403, "Sender does not match the agent credential")
    sender = await db.interactions.attribution(body.from_entity, registration, owner_origin)
    if sender["attribution"] == "unverified" and await signed_as(request, db, body.from_entity):
        sender["attribution"] = "signed"
    try:
        relations = await db.interactions.validate_relations(
            [r.model_dump() for r in supplied.relations] if supplied else [],
            registration,
            operator=operator,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "sender": sender,
        "body": body.message,
        "original_target": body.target_session,
        "relations": relations,
    }
