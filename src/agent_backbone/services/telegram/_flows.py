"""Prefect flows invoked by Telegram bot commands.

These flows provide structured, observable operations that can be triggered
via Telegram. The bot's command handlers call these flows for operations
that benefit from Prefect's retry/observability features.
"""

from __future__ import annotations

import logging

from agent_backbone.services._locator import get_db
from agent_backbone.services.terminal import list_sessions

log = logging.getLogger(__name__)


async def get_system_status() -> dict:
    """Get full system status for the /status and /digest commands.

    Returns dict with sessions, delivery stats, and agent states.
    """
    db = get_db()

    sessions = await list_sessions()

    failed = await db.get_failed_deliveries(limit=50)
    recent = await db.query_deliveries(limit=10)
    states = await db.get_all_agent_states()

    return {
        "sessions": sessions,
        "failed_count": len(failed),
        "recent_deliveries": recent,
        "agent_states": states,
    }


async def get_delivery_queue() -> dict:
    """Get delivery queue status for the /queue command."""
    db = get_db()

    failed = await db.get_failed_deliveries(limit=20)
    recent = await db.query_deliveries(limit=10)

    return {
        "failed": failed,
        "recent": recent,
    }
