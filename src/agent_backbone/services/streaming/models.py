"""Streaming data models — control mode events and connection state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class OutputEvent:
    """Output from a pane (%output)."""

    pane_id: str
    data: str


@dataclass(frozen=True)
class ModeChangeEvent:
    """Pane mode changed (%pane-mode-changed)."""

    pane_id: str


@dataclass(frozen=True)
class LayoutChangeEvent:
    """Window layout changed (%layout-change)."""

    window_id: str
    layout: str


@dataclass(frozen=True)
class WindowEvent:
    """Window added or closed (%window-add / %window-close)."""

    window_id: str
    action: str


@dataclass(frozen=True)
class SessionEvent:
    """Session changed (%session-changed)."""

    session_id: str
    name: str


@dataclass(frozen=True)
class ExitEvent:
    """Control mode exit (%exit)."""

    reason: str


ControlModeEvent = (
    OutputEvent | ModeChangeEvent | LayoutChangeEvent | WindowEvent | SessionEvent | ExitEvent
)


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
