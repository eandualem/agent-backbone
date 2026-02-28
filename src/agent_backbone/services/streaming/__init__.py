"""Streaming service — control mode, SSE broker, and PTY management."""

from agent_backbone.services.streaming._broker import (
    SessionBroadcast,
    StreamBroker,
    _event_to_data,
    _event_type_name,
    format_sse,
    parse_sse_frame,
)
from agent_backbone.services.streaming._control_mode import (
    ControlModeConnection,
    ControlModeManager,
    _manager,
    _unescape_output,
    parse_control_mode_line,
)
from agent_backbone.services.streaming._pty import PtyManager, PtySession
from agent_backbone.services.streaming.exceptions import StreamingServiceError
from agent_backbone.services.streaming.interface import StreamingService
from agent_backbone.services.streaming.models import (
    ConnectionState,
    ControlModeEvent,
    ExitEvent,
    LayoutChangeEvent,
    ModeChangeEvent,
    OutputEvent,
    SessionEvent,
    WindowEvent,
)

__all__ = [
    "ConnectionState",
    "ControlModeConnection",
    "ControlModeEvent",
    "ControlModeManager",
    "ExitEvent",
    "LayoutChangeEvent",
    "ModeChangeEvent",
    "OutputEvent",
    "PtyManager",
    "PtySession",
    "SessionBroadcast",
    "SessionEvent",
    "StreamBroker",
    "StreamingService",
    "StreamingServiceError",
    "WindowEvent",
    "_event_to_data",
    "_event_type_name",
    "_manager",
    "_unescape_output",
    "format_sse",
    "parse_control_mode_line",
    "parse_sse_frame",
]
