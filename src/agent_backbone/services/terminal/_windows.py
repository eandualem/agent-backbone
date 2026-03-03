"""Tmux window operations — create, close, rename, list, select, layout."""

from __future__ import annotations

import asyncio
import logging

from agent_backbone.services.terminal._core import send_message
from agent_backbone.services.terminal._panes import list_panes, split_pane
from agent_backbone.services.terminal.models import WindowInfo

log = logging.getLogger(__name__)


async def create_window(
    session_name: str,
    *,
    name: str | None = None,
    command: str | None = None,
) -> bool:
    """Create a new window in a tmux session."""
    args = ["tmux", "new-window", "-t", session_name]
    if name:
        args.extend(["-n", name])
    if command:
        args.append(command)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("create-window failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def close_window(session_name: str, window_id: str) -> bool:
    """Close (kill) a window in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-window",
        "-t",
        f"{session_name}:{window_id}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("close-window failed for '%s:%s': %s", session_name, window_id, stderr.decode())
        return False
    return True


async def rename_window(session_name: str, window_id: str, new_name: str) -> bool:
    """Rename a window in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "rename-window",
        "-t",
        f"{session_name}:{window_id}",
        new_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("rename-window failed for '%s:%s': %s", session_name, window_id, stderr.decode())
        return False
    return True


async def list_windows(session_name: str) -> list[WindowInfo]:
    """List windows in a tmux session with metadata."""
    fmt = "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{window_panes}"
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-windows",
        "-t",
        session_name,
        "-F",
        fmt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    results: list[WindowInfo] = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        results.append(
            WindowInfo(
                window_id=parts[0],
                window_index=parts[1],
                window_name=parts[2],
                window_active=parts[3] == "1",
                window_panes=int(parts[4]) if parts[4].isdigit() else 0,
            )
        )
    return results


async def select_window(session_name: str, window_id: str) -> bool:
    """Select (focus) a window in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "select-window",
        "-t",
        f"{session_name}:{window_id}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("select-window failed for '%s:%s': %s", session_name, window_id, stderr.decode())
        return False
    return True


async def set_layout(session_name: str, layout: str) -> bool:
    """Apply a tmux layout preset to a session.

    Common layouts: 'even-horizontal', 'even-vertical', 'main-horizontal',
    'main-vertical', 'tiled'.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "select-layout",
        "-t",
        session_name,
        layout,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("set-layout failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def create_layout(
    session_name: str,
    layout_spec: dict,
) -> bool:
    """Create a multi-pane layout from a spec.

    Args:
        session_name: Target tmux session.
        layout_spec: Dict with keys:
            - panes (int): Number of panes (must be >= 1).
            - layout (str): Layout preset to apply after splitting.
            - commands (list[str] | None): Optional commands to send to each pane.

    Creates panes by splitting N-1 times, applies the layout preset,
    then optionally sends commands to each pane.
    """
    pane_count = layout_spec.get("panes", 1)
    layout = layout_spec.get("layout", "tiled")
    commands = layout_spec.get("commands")

    # Split N-1 times to create the desired number of panes
    for _ in range(pane_count - 1):
        if not await split_pane(session_name):
            log.error("create_layout: split failed for '%s', aborting", session_name)
            return False

    # Apply layout preset
    if not await set_layout(session_name, layout):
        log.error("create_layout: set_layout failed for '%s'", session_name)
        return False

    # Send commands to panes if provided
    if commands:
        panes = await list_panes(session_name)
        for i, cmd in enumerate(commands):
            if i < len(panes) and cmd:
                await send_message(f"{session_name}:{panes[i]['pane_index']}", cmd)

    return True
