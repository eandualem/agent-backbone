"""Tmux pipe-pane streaming — start and stop pane output piping."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


async def start_pipe_pane(session_name: str, output_path: str) -> bool:
    """Start piping pane output to a file.

    Runs `tmux pipe-pane -t {session} -o 'cat >> {path}'`.
    Returns True if the command succeeded, False otherwise.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "pipe-pane",
        "-t",
        session_name,
        "-o",
        f"cat >> {output_path}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("pipe-pane start failed for '%s': %s", session_name, stderr.decode())
        return False
    log.info("Started pipe-pane for '%s' → %s", session_name, output_path)
    return True


async def stop_pipe_pane(session_name: str) -> bool:
    """Stop piping pane output.

    Runs `tmux pipe-pane -t {session}` (no -o disconnects the pipe).
    Returns True if the command succeeded, False otherwise.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "pipe-pane",
        "-t",
        session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("pipe-pane stop failed for '%s': %s", session_name, stderr.decode())
        return False
    log.info("Stopped pipe-pane for '%s'", session_name)
    return True
