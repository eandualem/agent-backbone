"""Terminal service — async tmux session operations and PTY management."""

from agent_backbone.services.terminal._adapters import (
    AGENT_ENV_KEY,
    RUNTIME_ENV_KEY,
    STATE_DIR_ENV_KEY,
    TerminalRuntime,
    detect_runtime_from_pane,
    get_terminal_adapter,
    get_terminal_adapter_for_session,
    infer_state_from_pane,
    prompt_has_pending_input,
    resolve_terminal_runtime,
    sanitize_pane_content,
)
from agent_backbone.services.terminal._copy_mode import clear_copy_mode, handle_copy_mode_recovery
from agent_backbone.services.terminal._core import (
    capture_pane,
    get_window_size,
    resize_window,
    send_keys,
    send_message,
    session_exists,
    set_window_size_mode,
)
from agent_backbone.services.terminal._panes import (
    close_pane,
    list_panes,
    resize_pane,
    split_pane,
    swap_panes,
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
from agent_backbone.services.terminal._streaming import start_pipe_pane, stop_pipe_pane
from agent_backbone.services.terminal._windows import (
    close_window,
    create_layout,
    create_window,
    list_windows,
    rename_window,
    select_window,
    set_layout,
)
from agent_backbone.services.terminal.exceptions import TmuxError
from agent_backbone.services.terminal.interface import TmuxService
from agent_backbone.services.terminal.models import (
    PaneInfo,
    WindowInfo,
)

__all__ = [
    "PaneInfo",
    "PtyManager",
    "PtySession",
    "AGENT_ENV_KEY",
    "RUNTIME_ENV_KEY",
    "STATE_DIR_ENV_KEY",
    "SESSION_FORMAT_STR",
    "TerminalRuntime",
    "TmuxError",
    "TmuxService",
    "WindowInfo",
    "capture_pane",
    "clear_copy_mode",
    "close_pane",
    "close_window",
    "create_layout",
    "create_window",
    "detect_runtime_from_pane",
    "get_window_size",
    "graceful_close",
    "handle_copy_mode_recovery",
    "infer_state_from_pane",
    "list_panes",
    "list_sessions",
    "list_sessions_rich",
    "list_windows",
    "get_terminal_adapter",
    "get_terminal_adapter_for_session",
    "prompt_has_pending_input",
    "query_environment_var",
    "query_format_vars",
    "rename_window",
    "resize_pane",
    "resize_window",
    "resolve_terminal_runtime",
    "sanitize_pane_content",
    "select_window",
    "send_keys",
    "send_message",
    "session_exists",
    "set_window_size_mode",
    "set_layout",
    "split_pane",
    "start_pipe_pane",
    "start_session",
    "stop_pipe_pane",
    "stop_session",
    "swap_panes",
]
