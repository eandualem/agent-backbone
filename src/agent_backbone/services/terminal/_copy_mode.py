"""Observe tmux copy mode without changing human scrollback or selection."""

from __future__ import annotations

from agent_backbone.services.terminal._sessions import query_format_vars


async def in_copy_mode(session_name: str) -> bool:
    """Whether the pane is in a mode that consumes input before the runtime."""
    tmux_vars = await query_format_vars(session_name, "pane_in_mode=#{pane_in_mode}")
    return tmux_vars.get("pane_in_mode") == "1"
