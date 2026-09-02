"""Terminal service — async tmux primitives and PTY streaming. A leaf: it
knows sessions, panes and keys, never which program runs inside."""

from agent_backbone.services.terminal._copy_mode import clear_copy_mode
from agent_backbone.services.terminal._core import (
    active_pane_size,
    capture_pane,
    paste_message,
    press_escape,
    press_submit,
    resize_window,
    send_keys,
    session_exists,
    set_window_size_mode,
)
from agent_backbone.services.terminal._pty import PtyManager, PtySession
from agent_backbone.services.terminal._sessions import (
    SESSION_FORMAT_STR,
    graceful_close,
    list_sessions,
    list_sessions_rich,
    query_environment_var,
    query_format_vars,
    start_session,
    stop_session,
)
from agent_backbone.services.terminal.interface import TmuxService

__all__ = [
    "SESSION_FORMAT_STR",
    "PtyManager",
    "PtySession",
    "TmuxService",
    "active_pane_size",
    "capture_pane",
    "clear_copy_mode",
    "graceful_close",
    "list_sessions",
    "list_sessions_rich",
    "paste_message",
    "press_escape",
    "press_submit",
    "query_environment_var",
    "query_format_vars",
    "resize_window",
    "send_keys",
    "session_exists",
    "set_window_size_mode",
    "start_session",
    "stop_session",
]
