"""Room management endpoints for group meetings."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, get_db, get_delivery_service
from agent_backbone.api.models import (
    BroadcastMessageRequest,
    DirectedMessageRequest,
    ListEnvelope,
    ResponseMessageRequest,
    Room,
    RoomCreate,
    RoomMessage,
    RoomStateUpdate,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing import DeliveryService, resolve_entity_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["rooms"])

_ROOM_DIR = Path.home() / ".claude" / "state" / "rooms"
_MEETING_SKILL_PATH = Path.home() / ".claude" / "skills" / "meeting-participant" / "SKILL.md"

_VALID_STATES = {"active", "paused", "closed"}


# --- Storage helpers ---


async def _load_room(room_id: str) -> Room | None:
    """Load a room from its JSON file. Returns None if not found."""
    path = _ROOM_DIR / f"{room_id}.json"
    if not path.exists():
        return None
    try:
        text = await asyncio.to_thread(path.read_text)
        return Room.model_validate_json(text)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


async def _save_room(room: Room) -> None:
    """Write a room to its JSON file, updating updated_at."""
    _ROOM_DIR.mkdir(parents=True, exist_ok=True)
    room.updated_at = _now_iso()
    path = _ROOM_DIR / f"{room.id}.json"
    await asyncio.to_thread(path.write_text, room.model_dump_json(indent=2))


async def _list_rooms(state_filter: str | None = None) -> list[Room]:
    """List all rooms, optionally filtered by state."""
    if not _ROOM_DIR.exists():
        return []
    rooms: list[Room] = []
    paths = await asyncio.to_thread(lambda: list(_ROOM_DIR.glob("*.json")))
    for path in paths:
        try:
            text = await asyncio.to_thread(path.read_text)
            room = Room.model_validate_json(text)
            if state_filter is None or room.state == state_filter:
                rooms.append(room)
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return rooms


def _compute_context_delta(room: Room, participant: str) -> list[RoomMessage]:
    """Return transcript messages since the participant's last delivery cursor."""
    cursor = room.cursors.get(participant, 0)
    return room.transcript[cursor:]


def _format_context_section(messages: list[RoomMessage]) -> str:
    """Format messages as '[sender]: content' lines."""
    return "\n".join(f"[{m.sender}]: {m.content}" for m in messages)


def _format_room_message(
    room: Room,
    sender: str,
    content: str,
    participant: str,
    context_delta: list[RoomMessage],
) -> str:
    """Format the full tmux delivery envelope."""
    parts = [f"[via:room room:{room.id} from:{sender}]"]
    parts.append(f"\nTitle: {room.title}")
    if room.description:
        parts.append(f"Description: {room.description}")

    if context_delta:
        parts.append("\n--- Context (since your last participation) ---")
        parts.append(_format_context_section(context_delta))

    parts.append(f"\n[meeting: {room.title}] [from: {sender}]")
    parts.append(content)

    return "\n".join(parts)


def _check_room_active(room: Room) -> None:
    """Raise 400 if the room is not in an active state."""
    if room.state == "closed":
        raise HTTPException(status_code=400, detail="Cannot send messages to a closed room")
    if room.state == "paused":
        raise HTTPException(status_code=400, detail="Room is paused")


# --- Meeting skill injection ---


def _read_meeting_skill() -> str | None:
    """Read the meeting-participant skill file. Returns content or None."""
    try:
        return _MEETING_SKILL_PATH.read_text()
    except FileNotFoundError:
        log.warning("Meeting skill not found at %s", _MEETING_SKILL_PATH)
        return None
    except OSError as exc:
        log.warning("Failed to read meeting skill: %s", exc)
        return None


def _format_meeting_setup_envelope(room: Room, skill_content: str) -> str:
    """Format the meeting-setup envelope per ROOM-18."""
    participant_list = ", ".join(room.participants)
    return (
        f"[via:meeting-setup room:{room.id}]\n"
        f'You are joining meeting: "{room.title}"\n'
        f"Moderator: {room.moderator}\n"
        f"Participants: {participant_list}\n"
        f"\n--- Meeting Participant Protocol ---\n"
        f"{skill_content}\n"
        f"---\n"
        f"\n--- How to Reply ---\n"
        f"To respond in this meeting, run:\n"
        f"~/.claude/bin/room-respond.sh --room {room.id} "
        f'--from {{your_name}} --message "your response"\n'
        f"\n"
        f"Replace {{your_name}} with your entity name. "
        f"Always reply to the room — do not just respond in your terminal.\n"
        f"---"
    )


async def _inject_meeting_skill(
    room: Room,
    config: BackboneConfig,
    db: BackboneDB,
    delivery_svc: DeliveryService,
) -> None:
    """Deliver meeting-participant skill to all room participants. Fire-and-forget."""
    skill_content = await asyncio.to_thread(_read_meeting_skill)
    if skill_content is None:
        return

    envelope = _format_meeting_setup_envelope(room, skill_content)

    async def _deliver(participant: str) -> bool:
        session_name = await resolve_entity_session(participant, config)
        if session_name is None:
            log.warning(
                "Meeting skill injection: cannot resolve session for '%s'",
                participant,
            )
            return False
        result = await delivery_svc.safe_deliver(
            session_name,
            envelope,
            config,
            db=db,
            delivery_kind="direct_message",
            flow_name="room-inject-meeting-skill",
        )
        if result != "delivered":
            log.warning("Meeting skill injection failed for '%s': %s", participant, result)
            return False
        return True

    results = await asyncio.gather(
        *[_deliver(p) for p in room.participants],
        return_exceptions=True,
    )
    changed = False
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            log.warning(
                "Meeting skill injection exception for '%s': %s",
                room.participants[i],
                r,
            )
        elif r is True:
            room.cursors[room.participants[i]] = len(room.transcript)
            changed = True
    if changed:
        await _save_room(room)


# --- Endpoints ---


@router.post("/rooms", response_model=Room, status_code=201)
async def create_room(
    body: RoomCreate,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Create a new meeting room."""
    now = _now_iso()
    room = Room(
        id=str(uuid.uuid4()),
        title=body.title,
        description=body.description,
        moderator=body.moderator,
        participants=body.participants,
        state="active",
        transcript=[],
        created_at=now,
        updated_at=now,
    )
    await _save_room(room)
    asyncio.create_task(_inject_meeting_skill(room, config, db, delivery_svc))
    return room


@router.get("/rooms", response_model=ListEnvelope[Room])
async def list_rooms(state: str | None = None):
    """List all rooms, optionally filtered by state."""
    rooms = await _list_rooms(state_filter=state)
    return ListEnvelope(items=rooms, total=len(rooms))


@router.get("/rooms/{room_id}", response_model=Room)
async def get_room(room_id: str):
    """Get a room with its full transcript."""
    room = await _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@router.post("/rooms/{room_id}/directed")
async def send_directed(
    room_id: str,
    body: DirectedMessageRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Send a directed message from any room member to another."""
    room = await _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    _check_room_active(room)

    # Determine effective sender (defaults to moderator when empty)
    effective_sender = body.sender or room.moderator

    # Validate sender is moderator or participant
    if effective_sender != room.moderator and effective_sender not in room.participants:
        raise HTTPException(status_code=400, detail="Sender is not a room participant or moderator")

    # Validate target is moderator or participant
    if body.target != room.moderator and body.target not in room.participants:
        raise HTTPException(status_code=400, detail="Target is not a room participant or moderator")

    # Can't message yourself
    if effective_sender == body.target:
        raise HTTPException(status_code=400, detail="Sender and target must be different")

    # Build recipient list: target + moderator CC (when non-moderator sends)
    deliver_to = [body.target]
    if effective_sender != room.moderator and room.moderator != body.target:
        deliver_to.append(room.moderator)

    # Deliver to all recipients in parallel
    async def _deliver_one(recipient: str) -> tuple[str, str]:
        session_name = await resolve_entity_session(recipient, config)
        if session_name is None:
            log.error("Room directed: cannot resolve session for '%s'", recipient)
            return (recipient, "unresolvable")
        delta = _compute_context_delta(room, recipient)
        envelope = _format_room_message(room, effective_sender, body.content, recipient, delta)
        result = await delivery_svc.safe_deliver(
            session_name,
            envelope,
            config,
            db=db,
            delivery_kind="direct_message",
            flow_name="room-send-directed",
        )
        if result != "delivered":
            log.error("Room directed delivery failed for '%s': %s", recipient, result)
        return (recipient, result)

    raw_results = await asyncio.gather(
        *[_deliver_one(r) for r in deliver_to],
        return_exceptions=True,
    )

    # Record in transcript (recipients = [target] only, not CC)
    msg = RoomMessage(
        id=str(uuid.uuid4()),
        sender=effective_sender,
        recipients=[body.target],
        mode="directed",
        content=body.content,
        timestamp=_now_iso(),
    )
    room.transcript.append(msg)

    # Advance cursors for delivered recipients + sender
    for r in raw_results:
        if isinstance(r, tuple) and r[1] == "delivered":
            room.cursors[r[0]] = len(room.transcript)
    # Sender knows what they sent — advance their cursor too
    room.cursors[effective_sender] = len(room.transcript)

    await _save_room(room)

    # Primary outcome = target delivery result
    target_result = "error"
    for r in raw_results:
        if isinstance(r, tuple) and r[0] == body.target:
            target_result = r[1]
            break
        elif isinstance(r, BaseException):
            target_result = "error"

    return {
        "ok": target_result == "delivered",
        "status": target_result,
        "target": body.target,
        "sender": effective_sender,
    }


@router.post("/rooms/{room_id}/broadcast")
async def send_broadcast(
    room_id: str,
    body: BroadcastMessageRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Broadcast a message to all participants with per-participant context deltas."""
    room = await _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    _check_room_active(room)

    # Deliver to each participant in parallel via session bridge
    async def _deliver(participant: str) -> tuple[str, str]:
        """Returns (participant, outcome)."""
        session_name = await resolve_entity_session(participant, config)
        if session_name is None:
            log.error(
                "Room broadcast: cannot resolve session for participant '%s'",
                participant,
            )
            return (participant, "unresolvable")
        delta = _compute_context_delta(room, participant)
        envelope = _format_room_message(
            room,
            room.moderator,
            body.content,
            participant,
            delta,
        )
        result = await delivery_svc.safe_deliver(
            session_name,
            envelope,
            config,
            db=db,
            delivery_kind="direct_message",
            flow_name="room-send-broadcast",
        )
        if result != "delivered":
            log.error(
                "Room broadcast delivery failed for '%s' (session '%s'): %s",
                participant,
                session_name,
                result,
            )
        return (participant, result)

    raw_results = await asyncio.gather(
        *[_deliver(p) for p in room.participants],
        return_exceptions=True,
    )

    # Process results — log exceptions, count outcomes
    delivered = 0
    for i, r in enumerate(raw_results):
        participant = room.participants[i]
        if isinstance(r, BaseException):
            log.error(
                "Room broadcast exception for '%s': %s",
                participant,
                r,
            )
        elif isinstance(r, tuple) and r[1] == "delivered":
            delivered += 1
    failed = len(room.participants) - delivered

    # Record in transcript regardless
    msg = RoomMessage(
        id=str(uuid.uuid4()),
        sender=room.moderator,
        recipients=list(room.participants),
        mode="broadcast",
        content=body.content,
        timestamp=_now_iso(),
    )
    room.transcript.append(msg)

    # Advance cursors for successfully delivered participants
    for r in raw_results:
        if isinstance(r, tuple) and r[1] == "delivered":
            room.cursors[r[0]] = len(room.transcript)

    await _save_room(room)

    return {
        "ok": failed == 0,
        "delivered": delivered,
        "failed": failed,
        "total": len(room.participants),
    }


@router.post("/rooms/{room_id}/respond")
async def post_response(
    room_id: str,
    body: ResponseMessageRequest,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Post a participant response and deliver to moderator."""
    room = await _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    _check_room_active(room)

    if body.sender != room.moderator and body.sender not in room.participants:
        raise HTTPException(status_code=400, detail="Sender is not a room participant or moderator")

    msg = RoomMessage(
        id=str(uuid.uuid4()),
        sender=body.sender,
        recipients=[],
        mode="response",
        content=body.content,
        timestamp=_now_iso(),
    )
    room.transcript.append(msg)

    # Sender knows what they sent — advance their cursor
    room.cursors[body.sender] = len(room.transcript)

    # Deliver to moderator if sender is not the moderator
    if body.sender != room.moderator:
        session_name = await resolve_entity_session(room.moderator, config)
        if session_name is None:
            log.warning(
                "Room respond: cannot resolve session for moderator '%s'",
                room.moderator,
            )
        else:
            delta = _compute_context_delta(room, room.moderator)
            envelope = _format_room_message(room, body.sender, body.content, room.moderator, delta)
            result = await delivery_svc.safe_deliver(
                session_name,
                envelope,
                config,
                db=db,
                delivery_kind="direct_message",
                flow_name="room-post-response",
            )
            if result == "delivered":
                room.cursors[room.moderator] = len(room.transcript)
            else:
                log.warning(
                    "Room respond: moderator delivery failed for '%s': %s",
                    room.moderator,
                    result,
                )

    await _save_room(room)

    return {"ok": True}


@router.patch("/rooms/{room_id}", response_model=Room)
async def update_room_state(
    room_id: str,
    body: RoomStateUpdate,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Update room state (active/paused/closed)."""
    if body.state not in _VALID_STATES:
        valid = ", ".join(sorted(_VALID_STATES))
        raise HTTPException(
            status_code=422,
            detail=f"Invalid state '{body.state}'. Must be one of: {valid}",
        )

    room = await _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    old_state = room.state
    room.state = body.state
    await _save_room(room)

    if body.state == "active" and old_state != "active":
        asyncio.create_task(_inject_meeting_skill(room, config, db, delivery_svc))

    return room
