"""Room management endpoints for group meetings."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.models import (
    BroadcastMessageRequest,
    DirectedMessageRequest,
    ListEnvelope,
    ResponseMessageRequest,
    Room,
    RoomCreate,
    RoomMessage,
    RoomStateUpdate,
)
from src.tmux import send_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["rooms"])

_ROOM_DIR = Path.home() / ".claude" / "state" / "rooms"

_VALID_STATES = {"active", "paused", "closed"}


# --- Storage helpers ---


def _load_room(room_id: str) -> Room | None:
    """Load a room from its JSON file. Returns None if not found."""
    path = _ROOM_DIR / f"{room_id}.json"
    if not path.exists():
        return None
    try:
        return Room.model_validate_json(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _save_room(room: Room) -> None:
    """Write a room to its JSON file, updating updated_at."""
    _ROOM_DIR.mkdir(parents=True, exist_ok=True)
    room.updated_at = time.time()
    path = _ROOM_DIR / f"{room.id}.json"
    path.write_text(room.model_dump_json(indent=2))


def _list_rooms(state_filter: str | None = None) -> list[Room]:
    """List all rooms, optionally filtered by state."""
    if not _ROOM_DIR.exists():
        return []
    rooms: list[Room] = []
    for path in _ROOM_DIR.glob("*.json"):
        try:
            room = Room.model_validate_json(path.read_text())
            if state_filter is None or room.state == state_filter:
                rooms.append(room)
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return rooms


def _compute_context_delta(room: Room, participant: str) -> list[RoomMessage]:
    """Return messages since the participant's last response.

    Scans transcript backward for the participant's last 'response' message.
    Returns everything after it. If never responded, returns full transcript.
    """
    last_response_idx = -1
    for i in range(len(room.transcript) - 1, -1, -1):
        msg = room.transcript[i]
        if msg.sender == participant and msg.mode == "response":
            last_response_idx = i
            break
    return room.transcript[last_response_idx + 1 :]


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

    parts.append("\n--- Current prompt ---")
    parts.append(content)

    return "\n".join(parts)


# --- Endpoints ---


@router.post("/rooms", response_model=Room, status_code=201)
async def create_room(body: RoomCreate):
    """Create a new meeting room."""
    now = time.time()
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
    _save_room(room)
    return room


@router.get("/rooms", response_model=ListEnvelope[Room])
async def list_rooms(state: str | None = None):
    """List all rooms, optionally filtered by state."""
    rooms = _list_rooms(state_filter=state)
    return ListEnvelope(items=rooms, total=len(rooms))


@router.get("/rooms/{room_id}", response_model=Room)
async def get_room(room_id: str):
    """Get a room with its full transcript."""
    room = _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@router.post("/rooms/{room_id}/directed")
async def send_directed(room_id: str, body: DirectedMessageRequest):
    """Send a directed message to one participant with context delta."""
    room = _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    if body.target not in room.participants:
        raise HTTPException(status_code=400, detail="Target is not a room participant")

    # Compute context delta for the target
    delta = _compute_context_delta(room, body.target)

    # Format and deliver via tmux
    envelope = _format_room_message(room, room.moderator, body.content, body.target, delta)
    delivered = await send_message(body.target, envelope)

    # Record in transcript regardless of delivery outcome
    msg = RoomMessage(
        id=str(uuid.uuid4()),
        sender=room.moderator,
        recipients=[body.target],
        mode="directed",
        content=body.content,
        timestamp=time.time(),
    )
    room.transcript.append(msg)
    _save_room(room)

    return {"ok": delivered, "delivered": 1 if delivered else 0, "target": body.target}


@router.post("/rooms/{room_id}/broadcast")
async def send_broadcast(room_id: str, body: BroadcastMessageRequest):
    """Broadcast a message to all participants with per-participant context deltas."""
    room = _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    # Deliver to each participant in parallel
    async def _deliver(participant: str) -> bool:
        delta = _compute_context_delta(room, participant)
        envelope = _format_room_message(room, room.moderator, body.content, participant, delta)
        return await send_message(participant, envelope)

    results = await asyncio.gather(
        *[_deliver(p) for p in room.participants], return_exceptions=True
    )

    delivered = sum(1 for r in results if r is True)
    failed = len(room.participants) - delivered

    # Record in transcript regardless
    msg = RoomMessage(
        id=str(uuid.uuid4()),
        sender=room.moderator,
        recipients=list(room.participants),
        mode="broadcast",
        content=body.content,
        timestamp=time.time(),
    )
    room.transcript.append(msg)
    _save_room(room)

    return {
        "ok": failed == 0,
        "delivered": delivered,
        "failed": failed,
        "total": len(room.participants),
    }


@router.post("/rooms/{room_id}/respond")
async def post_response(room_id: str, body: ResponseMessageRequest):
    """Post a participant response to the room transcript."""
    room = _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    msg = RoomMessage(
        id=str(uuid.uuid4()),
        sender=body.sender,
        recipients=[],
        mode="response",
        content=body.content,
        timestamp=time.time(),
    )
    room.transcript.append(msg)
    _save_room(room)

    return {"ok": True}


@router.patch("/rooms/{room_id}", response_model=Room)
async def update_room_state(room_id: str, body: RoomStateUpdate):
    """Update room state (active/paused/closed)."""
    if body.state not in _VALID_STATES:
        valid = ", ".join(sorted(_VALID_STATES))
        raise HTTPException(
            status_code=422,
            detail=f"Invalid state '{body.state}'. Must be one of: {valid}",
        )

    room = _load_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    room.state = body.state
    _save_room(room)
    return room
