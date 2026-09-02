"""Leaving tmux copy mode.

Copy mode (scrolling, mouse selection) freezes the pane: pasted text is not
seen by the runtime. Delivery clears it right before a paste; the monitor
job clears it every tick and alerts when it will not go.
"""

from __future__ import annotations

import asyncio
import logging

from agent_backbone.services.terminal._core import _run_tmux
from agent_backbone.services.terminal._sessions import query_format_vars

log = logging.getLogger(__name__)

_RECHECK_DELAY_SECONDS = 0.1


async def in_copy_mode(session_name: str) -> bool:
    tmux_vars = await query_format_vars(session_name, "pane_in_mode=#{pane_in_mode}")
    return tmux_vars.get("pane_in_mode") == "1"


async def cancel_copy_mode(session_name: str) -> bool:
    """Ask tmux to leave copy mode now. True when the command was accepted."""
    rc, _, stderr = await _run_tmux("send-keys", "-X", "-t", session_name, "cancel")
    if rc == 0 or "not in a mode" in stderr.decode().strip().lower():
        return True
    log.error("tmux copy-mode cancel failed for '%s': %s", session_name, stderr.decode())
    return False


async def clear_copy_mode(session_name: str) -> tuple[bool, bool]:
    """``(was_in_copy_mode, cleared)`` — cancels copy mode when the pane is in it."""
    if not await in_copy_mode(session_name):
        return False, True
    log.info("Clearing tmux copy mode in %s", session_name)
    if await cancel_copy_mode(session_name):
        await asyncio.sleep(_RECHECK_DELAY_SECONDS)
    return True, not await in_copy_mode(session_name)
