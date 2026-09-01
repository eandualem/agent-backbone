"""Copy-mode auto-clear for tmux-backed agent sessions.

tmux copy mode (scrolling, mouse selection) freezes the pane: pasted text
is not seen by the runtime. It is a defect, not an agent state, so the
backbone cancels it wherever it finds it — here on every monitor tick and
in the delivery path right before a paste.
"""

from __future__ import annotations

import asyncio
import logging
import time

from agent_backbone.config import BackboneConfig
from agent_backbone.services.terminal._adapters import get_terminal_adapter_for_session
from agent_backbone.services.terminal._sessions import query_format_vars

log = logging.getLogger(__name__)

_RECHECK_DELAY_SECONDS = 0.1
_ALERT_DEDUP_SECONDS = 1800
_alerted_at: dict[str, float] = {}


class TelegramService:
    """Lazy proxy to avoid importing telegram service during module import."""

    @staticmethod
    async def send_notification(*args, **kwargs):
        from agent_backbone.services.telegram.interface import TelegramService as _TelegramService

        return await _TelegramService.send_notification(*args, **kwargs)


async def in_copy_mode(session_name: str) -> bool:
    tmux_vars = await query_format_vars(session_name, "pane_in_mode=#{pane_in_mode}")
    return tmux_vars.get("pane_in_mode") == "1"


async def clear_copy_mode(session_name: str) -> bool:
    """Cancel copy mode if active. Returns True when the pane is out of copy mode."""
    if not await in_copy_mode(session_name):
        return True
    adapter = await get_terminal_adapter_for_session(session_name)
    log.info("Clearing tmux copy mode in %s", session_name)
    if await adapter.exit_copy_mode(session_name):
        await asyncio.sleep(_RECHECK_DELAY_SECONDS)
    return not await in_copy_mode(session_name)


async def handle_copy_mode_recovery(config: BackboneConfig, active_sessions: set[str]) -> None:
    """Clear copy mode in every managed session; alert if it will not clear."""
    managed = sorted(s for s in active_sessions if s in config.agents)
    for stale in [s for s in _alerted_at if s not in managed]:
        del _alerted_at[stale]

    for session_name in managed:
        try:
            cleared = await clear_copy_mode(session_name)
        except Exception:
            log.exception("Copy-mode check failed for %s (non-fatal)", session_name)
            continue
        if cleared:
            _alerted_at.pop(session_name, None)
            continue

        last = _alerted_at.get(session_name)
        if last is not None and (time.monotonic() - last) < _ALERT_DEDUP_SECONDS:
            continue
        _alerted_at[session_name] = time.monotonic()
        log.warning("Copy mode persists in %s after cancel attempt", session_name)
        if config.telegram.notification_chat_id and config.telegram_token:
            await TelegramService.send_notification(
                config.telegram_token,
                config.telegram.notification_chat_id,
                f"Copy mode persists in {session_name}; the backbone could not clear it. "
                "Press q in that tmux pane.",
            )
