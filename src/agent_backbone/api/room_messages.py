"""Formatting and skill helpers for room delivery flows."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_backbone.api.models import Room, RoomMessage


def compute_context_delta(room: Room, participant: str) -> list[RoomMessage]:
    """Return transcript messages since the participant's last delivery cursor."""
    cursor = room.cursors.get(participant, 0)
    return room.transcript[cursor:]


def format_context_section(messages: list[RoomMessage]) -> str:
    """Format transcript entries as '[sender]: content' lines."""
    return "\n".join(f"[{message.sender}]: {message.content}" for message in messages)


def format_room_message(
    room: Room,
    sender: str,
    content: str,
    participant: str,
    context_delta: list[RoomMessage],
) -> str:
    """Format the delivery envelope for a room message."""
    parts = [f"[via:room room:{room.id} from:{sender}]"]
    parts.append(f"\nTitle: {room.title}")
    if room.description:
        parts.append(f"Description: {room.description}")

    if context_delta:
        parts.append("\n--- Context (since your last participation) ---")
        parts.append(format_context_section(context_delta))

    parts.append(f"\n[meeting: {room.title}] [from: {sender}]")
    parts.append(content)

    return "\n".join(parts)


def read_meeting_skill(skill_path: Path, *, logger: logging.Logger) -> str | None:
    """Read the meeting-participant skill file, logging non-fatal failures."""
    try:
        return skill_path.read_text()
    except FileNotFoundError:
        logger.warning("Meeting skill not found at %s", skill_path)
        return None
    except OSError as exc:
        logger.warning("Failed to read meeting skill: %s", exc)
        return None


def format_meeting_setup_envelope(room: Room, skill_content: str) -> str:
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
