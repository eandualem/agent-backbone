"""Terminal data models — tmux pane/window info."""

from __future__ import annotations

from typing import TypedDict


class PaneInfo(TypedDict):
    pane_id: str
    pane_index: str
    pane_width: str
    pane_height: str
    pane_active: bool


class WindowInfo(TypedDict):
    window_id: str
    window_index: str
    window_name: str
    window_active: bool
    window_panes: int
