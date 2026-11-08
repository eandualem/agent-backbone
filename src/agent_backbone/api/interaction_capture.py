"""Resolve explicit sender attribution before capturing a normal send."""

from fastapi import HTTPException


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
    signed = getattr(request.state, "signed_sender", None)
    if sender["attribution"] == "unverified" and signed and signed.sender == body.from_entity:
        # Verified by an enrolled key. Only an exact name: case-twin registrations can
        # exist, and the actor lookup above is exact, so a variant could name the other.
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
