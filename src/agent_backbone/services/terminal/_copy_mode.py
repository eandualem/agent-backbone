"""Leaving tmux copy mode.

Copy mode (scrolling, mouse selection) freezes the pane: pasted text is not
seen by the runtime. Delivery clears it right before a paste; the monitor
job clears it every tick and alerts when it will not go.
"""

from __future__ import annotations

import asyncio
import logging

from agent_backbone.services.terminal._adapters import get_terminal_adapter_for_session
from agent_backbone.services.terminal._sessions import query_format_vars

log = logging.getLogger(__name__)

_RECHECK_DELAY_SECONDS = 0.1


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
