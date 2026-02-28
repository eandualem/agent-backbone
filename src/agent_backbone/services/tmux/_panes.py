"""Tmux pane operations — list, split, resize, swap, close."""

from __future__ import annotations

import asyncio
import logging

from agent_backbone.services.tmux.models import PaneInfo

log = logging.getLogger(__name__)


async def list_panes(session_name: str) -> list[PaneInfo]:
    """List panes in a tmux session with metadata."""
    fmt = "#{pane_id}\t#{pane_index}\t#{pane_width}\t#{pane_height}\t#{pane_active}"
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-panes",
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
    results: list[PaneInfo] = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        results.append(
            PaneInfo(
                pane_id=parts[0],
                pane_index=parts[1],
                pane_width=parts[2],
                pane_height=parts[3],
                pane_active=parts[4] == "1",
            )
        )
    return results


async def split_pane(
    session_name: str,
    *,
    direction: str = "vertical",
    size: str | None = None,
    command: str | None = None,
    target_pane: str | None = None,
) -> bool:
    """Split a pane in a tmux session.

    Args:
        session_name: Target tmux session.
        direction: 'vertical' (-v) or 'horizontal' (-h).
        size: Size for the new pane (e.g. '50%', '20').
        command: Shell command to run in the new pane.
        target_pane: Specific pane to split (e.g. '0'). Defaults to active pane.
    """
    target = f"{session_name}:{target_pane}" if target_pane else session_name
    args = ["tmux", "split-window"]
    args.append("-v" if direction == "vertical" else "-h")
    if size:
        args.extend(["-l", size])
    args.extend(["-t", target])
    if command:
        args.append(command)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("split-pane failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def resize_pane(
    session_name: str,
    pane_id: str,
    *,
    width: int | None = None,
    height: int | None = None,
) -> bool:
    """Resize a pane in a tmux session.

    At least one of width or height must be provided.
    """
    args = ["tmux", "resize-pane", "-t", f"{session_name}:{pane_id}"]
    if width is not None:
        args.extend(["-x", str(width)])
    if height is not None:
        args.extend(["-y", str(height)])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("resize-pane failed for '%s:%s': %s", session_name, pane_id, stderr.decode())
        return False
    return True


async def swap_panes(session_name: str, pane_a: str, pane_b: str) -> bool:
    """Swap two panes in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "swap-pane",
        "-s",
        f"{session_name}:{pane_a}",
        "-t",
        f"{session_name}:{pane_b}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("swap-pane failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def close_pane(session_name: str, pane_id: str) -> bool:
    """Close (kill) a pane in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-pane",
        "-t",
        f"{session_name}:{pane_id}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("close-pane failed for '%s:%s': %s", session_name, pane_id, stderr.decode())
        return False
    return True
