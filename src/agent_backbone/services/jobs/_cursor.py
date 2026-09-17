"""Replay cursors shared by the pollers: one boundary per key in ``poll_cursors``."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

OVERLAP = timedelta(minutes=2)
"""Re-fetched behind every boundary for late arrivals; the events table dedups it."""


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("poll timestamps must include a timezone")
    return parsed.astimezone(UTC)


async def saved_cursor(db: BackboneDB, key: str, now: datetime, *, reset: str) -> datetime | None:
    """The persisted boundary for ``key``, or None when there is none or it is
    unusable (unparsable, or in the future) — ``reset`` says what happens then."""
    saved = await db.events.poll_cursor(key)
    if saved is None:
        return None
    try:
        boundary = parse(saved)
        if boundary > now:
            raise ValueError("poll cursor is in the future")
    except (TypeError, AttributeError, ValueError, OverflowError):
        log.warning("Invalid poll cursor for %s; %s", key, reset)
        return None
    return boundary
