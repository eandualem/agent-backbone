"""Storage helpers for API room state."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from agent_backbone.api.models import Room


def now_iso() -> str:
    """Return the current UTC timestamp as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


async def load_room(room_id: str, *, room_dir: Path) -> Room | None:
    """Load a room from disk, returning None when absent or invalid."""
    path = room_dir / f"{room_id}.json"
    if not path.exists():
        return None

    try:
        text = await asyncio.to_thread(path.read_text)
        return Room.model_validate_json(text)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


async def save_room(room: Room, *, room_dir: Path, now=now_iso) -> None:
    """Persist a room to disk and refresh its updated_at timestamp."""
    room_dir.mkdir(parents=True, exist_ok=True)
    room.updated_at = now()
    path = room_dir / f"{room.id}.json"
    await asyncio.to_thread(path.write_text, room.model_dump_json(indent=2))


async def list_rooms(*, room_dir: Path, state_filter: str | None = None) -> list[Room]:
    """List room JSON files, optionally filtering by room state."""
    if not room_dir.exists():
        return []

    rooms: list[Room] = []
    paths = await asyncio.to_thread(lambda: list(room_dir.glob("*.json")))
    for path in paths:
        try:
            text = await asyncio.to_thread(path.read_text)
            room = Room.model_validate_json(text)
        except (json.JSONDecodeError, OSError, ValueError):
            continue

        if state_filter is None or room.state == state_filter:
            rooms.append(room)

    return rooms
