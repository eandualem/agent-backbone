"""Observe tmux copy mode without changing human scrollback or selection."""

from __future__ import annotations

from agent_backbone.services.terminal._sessions import query_format_vars


async def in_copy_mode(session_name: str) -> bool:
    """Whether the pane is in a mode that consumes input before the runtime."""
    tmux_vars = await query_format_vars(session_name, "pane_in_mode=#{pane_in_mode}")
    return tmux_vars.get("pane_in_mode") == "1"


async def window_viewers(session_name: str) -> int | None:
    """How many clients are viewing the session's window, through any session
    in its group; ``None`` when tmux does not say. tmux keeps a pane in copy
    mode after its last client detaches."""
    tmux_vars = await query_format_vars(
        session_name, "window_active_clients=#{window_active_clients}"
    )
    value = tmux_vars.get("window_active_clients", "")
    return int(value) if value.isdigit() else None
