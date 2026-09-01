"""Vendor-neutral human notifications for callers without an integration instance.

Scheduler jobs (dead-session, plan-waiting, copy-mode alerts) run against a
configuration snapshot, not the running integration objects. ``notify_humans``
fans an alert out to every integration that is configured, using each one's
config-driven static sender. Adding an integration means adding one entry to
``_STATIC_NOTIFIERS`` — nothing in ``agents`` or ``terminal`` changes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

StaticNotifier = Callable[[BackboneConfig, str, str | None], Awaitable[bool]]


async def _telegram(config: BackboneConfig, text: str, agent: str | None) -> bool:
    from agent_backbone.services.integrations.telegram.interface import notify_static

    return await notify_static(config, text, agent=agent)


_STATIC_NOTIFIERS: dict[str, StaticNotifier] = {"telegram": _telegram}


async def notify_humans(config: BackboneConfig, text: str, *, agent: str | None = None) -> bool:
    """Send ``text`` to the humans on every configured integration.

    ``agent`` lets an integration route the alert into that agent's own
    surface (its Telegram topic) instead of the general alert destination.
    Returns True when at least one integration accepted it. Never raises:
    an alert that cannot be sent is logged, the caller's job goes on.
    """
    delivered = False
    for name, send in _STATIC_NOTIFIERS.items():
        try:
            if await send(config, text, agent):
                delivered = True
        except Exception:
            log.exception("%s notification failed (non-fatal)", name)
    return delivered
