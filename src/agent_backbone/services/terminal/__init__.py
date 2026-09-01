"""Terminal service — async tmux operations, runtime adapters and PTY streaming."""

from agent_backbone.services.terminal._adapters import (
    AGENT_ENV_KEY,
    RUNTIME_ENV_KEY,
    STATE_DIR_ENV_KEY,
    TerminalRuntime,
    detect_runtime_from_pane,
    get_terminal_adapter,
    get_terminal_adapter_for_session,
    normalize_runtime,
    resolve_terminal_runtime,
    sanitize_pane_content,
)
from agent_backbone.services.terminal._copy_mode import clear_copy_mode, handle_copy_mode_recovery
from agent_backbone.services.terminal._core import (
    active_pane_size,
    capture_pane,
    resize_window,
    send_keys,
    send_message,
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
    "AGENT_ENV_KEY",
    "RUNTIME_ENV_KEY",
    "SESSION_FORMAT_STR",
    "STATE_DIR_ENV_KEY",
    "PtyManager",
    "PtySession",
    "TerminalRuntime",
    "TmuxService",
    "active_pane_size",
    "capture_pane",
    "clear_copy_mode",
    "detect_runtime_from_pane",
    "get_terminal_adapter",
    "get_terminal_adapter_for_session",
    "graceful_close",
    "handle_copy_mode_recovery",
    "list_sessions",
    "list_sessions_rich",
    "normalize_runtime",
    "query_environment_var",
    "query_format_vars",
    "resize_window",
    "resolve_terminal_runtime",
    "sanitize_pane_content",
    "send_keys",
    "send_message",
    "session_exists",
    "set_window_size_mode",
    "start_session",
    "stop_session",
]
