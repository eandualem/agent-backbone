"""Lightweight message delivery — direct tmux/Jarvis delivery without queue gating."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

from agent_backbone.services.terminal import send_message, session_exists

log = logging.getLogger(__name__)


async def deliver_message(
    session_name: str,
    message: str,
    config: BackboneConfig,
    *,
    priority: bool = False,
) -> str:
    """Deliver a message to a tmux session or Jarvis HTTP target.

    Returns outcome string: "delivered", "offline", or "delivery_failed".
    No queue gating, no intelligence scoring, no issue tracking.
    """
    # Jarvis (HTTP target) — bypass tmux entirely
    if config.jarvis.inject_url and session_name == "jarvis":
        from agent_backbone.jarvis import inject_message

        if await inject_message(
            config.jarvis.inject_url,
            message,
            sessions_url=config.jarvis.sessions_url,
        ):
            return "delivered"
        return "delivery_failed"

    # tmux delivery
    if not await session_exists(session_name):
        return "offline"

    if await send_message(session_name, message):
        return "delivered"

    return "delivery_failed"
