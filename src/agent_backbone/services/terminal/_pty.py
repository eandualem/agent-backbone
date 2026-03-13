"""PTY-based terminal session management for interactive tmux access.

Spawns `tmux attach-session` in a PTY so that xterm.js receives the exact
same byte stream as a native terminal (Ghostty, iTerm). This gives correct
ANSI rendering, input echo via PTY line discipline, and proper SIGWINCH
resize propagation.

One PtySession per WebSocket connection (1:1 model). Each browser client
gets its own `tmux attach-session` process, delegating multiplexing to
tmux natively.
"""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import logging
import os
import signal
import struct
import subprocess
import termios
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_backbone.services.terminal._pty_pid_tracking import (
    append_pid,
    clear_orphaned_pids,
    remove_pid,
)

log = logging.getLogger(__name__)

# Max queued output chunks before dropping oldest
_MAX_QUEUE_SIZE = 1000

# PID tracking file for orphan cleanup on restart
_PID_FILE = Path("/tmp/agent-backbone-pty-pids.txt")


class PtySession:
    """Manages a PTY running `tmux attach-session`.

    The PTY master fd provides bidirectional communication:
    - Read: terminal output bytes (same as a real terminal receives)
    - Write: keyboard input bytes (with PTY echo handling)
    - Resize: TIOCSWINSZ ioctl sends SIGWINCH to tmux attach
    """

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self.master_fd: int | None = None
        self._process: subprocess.Popen | None = None
        self._reader_task: asyncio.Task | None = None
        # Single output queue for the 1:1 connection
        self._output_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        # Simple pause flag for flow control
        self._paused: bool = False
        # Event signalled when resumed (unblocks the read loop)
        self._resume_event: asyncio.Event = asyncio.Event()
        self._resume_event.set()  # Start unpaused
        # Dedicated thread pool so blocking os.read doesn't starve the default executor
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pty-{session_name}")
        # Incremental decoder handles multi-byte UTF-8 split across reads
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # Callback invoked on queue overflow: fn(session_name)
        self.on_data_dropped: None | (callable) = None

    @property
    def output_queue(self) -> asyncio.Queue[str | None]:
        """Read-only access to the output queue for forwarding."""
        return self._output_queue

    def start(self, cols: int = 80, rows: int = 24) -> None:
        """Spawn tmux attach-session in a PTY.

        Sets TERM=xterm-256color and COLORTERM=truecolor so tmux and
        programs like bat, delta, neovim emit 24-bit color escape sequences
        (matching what xterm.js expects).
        """
        master_fd, slave_fd = os.openpty()

        try:
            # Set initial terminal size before spawning
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

            env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
            env.pop("TMUX", None)  # Prevent tmux nesting guard when gateway runs inside tmux
            self._process = subprocess.Popen(
                ["tmux", "attach-session", "-t", self.session_name],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                preexec_fn=os.setsid,
                close_fds=True,
            )
        except Exception:
            os.close(slave_fd)
            os.close(master_fd)
            raise

        os.close(slave_fd)
        self.master_fd = master_fd

        # Track PID for orphan cleanup on restart
        _record_pid(self._process.pid)

        # Start background reader
        self._reader_task = asyncio.create_task(
            self._read_loop(),
            name=f"pty-reader-{self.session_name}",
        )
        log.info(
            "PTY started for '%s' (pid=%d, %dx%d)",
            self.session_name,
            self._process.pid,
            cols,
            rows,
        )

    async def _read_loop(self) -> None:
        """Read from PTY master and write to output queue.

        Respects pause state: when paused, stops reading from PTY fd
        (natural backpressure via kernel buffer). Resumes when client
        sends resume.
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                # Wait if paused
                await self._resume_event.wait()

                try:
                    data = await loop.run_in_executor(
                        self._executor,
                        os.read,
                        self.master_fd,
                        65536,
                    )
                except OSError:
                    break
                if not data:
                    break
                text = self._decoder.decode(data)
                try:
                    self._output_queue.put_nowait(text)
                except asyncio.QueueFull:
                    # Drop oldest for slow client
                    try:
                        self._output_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._output_queue.put_nowait(text)
                    except asyncio.QueueFull:
                        pass
                    # Notify about data loss
                    if self.on_data_dropped is not None:
                        try:
                            self.on_data_dropped(self.session_name)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            return
        finally:
            # Guarantee sentinel delivery — drain one slot if needed
            try:
                self._output_queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    self._output_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self._output_queue.put_nowait(None)
                except asyncio.QueueFull:
                    log.error(
                        "Cannot deliver PTY sentinel for '%s' — forwarding task may leak",
                        self.session_name,
                    )
            log.info("PTY read loop ended for '%s'", self.session_name)

    def write(self, data: str) -> None:
        """Write keyboard input to PTY master fd."""
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data.encode("utf-8"))
            except OSError:
                log.warning("PTY write failed for '%s'", self.session_name)

    def resize(self, cols: int, rows: int) -> None:
        """Resize PTY via TIOCSWINSZ ioctl — sends SIGWINCH to tmux attach.

        This only resizes the PTY file descriptor. The caller is responsible
        for also calling tmux resize-window if the tmux window itself should
        change dimensions.
        """
        if self.master_fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                log.warning("PTY resize failed for '%s'", self.session_name)

    def pause(self) -> None:
        """Pause output — client write buffer full."""
        self._paused = True
        self._resume_event.clear()

    def resume(self) -> None:
        """Resume output — client write buffer drained."""
        self._paused = False
        self._resume_event.set()

    async def cleanup(self) -> None:
        """Close PTY and terminate tmux attach process.

        Ordering matters to avoid FD races:
        1. Cancel reader task (stops new reads from being scheduled)
        2. Kill process (unblocks the os.read thread with EOF/OSError)
        3. Wait for executor to drain (read thread exits cleanly)
        4. Close FD (no threads accessing it)
        """
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()

        # Kill process first — this unblocks the os.read thread
        if self._process is not None:
            pid = self._process.pid
            proc = self._process
            self._process = None

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._terminate_process, proc)
            _unrecord_pid(pid)

        # Now safe to wait for the executor — the read thread should have exited
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._executor.shutdown(wait=True, cancel_futures=True),
        )

        # FD is safe to close — no threads reading it
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

        log.info("PTY cleaned up for '%s'", self.session_name)

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        """Blocking process termination — runs in thread executor."""
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


class PtyManager:
    """Manages PTY sessions — one per (sid, session_name) pair.

    In the 1:1 model, each WebSocket connection gets its own PTY process.
    No grace periods — disconnect kills the PTY immediately.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], PtySession] = {}
        self._cleanup_orphaned_processes()

    def _cleanup_orphaned_processes(self) -> None:
        """Kill orphaned tmux attach-session processes from previous instances.

        Uses a PID tracking file to only kill processes spawned by this
        application — not the user's own tmux attach terminals.
        """
        clear_orphaned_pids(_PID_FILE, kill_pid=os.kill, logger=log)

    async def create(
        self,
        sid: str,
        session_name: str,
        cols: int = 80,
        rows: int = 24,
    ) -> PtySession:
        """Create a new PTY session for this (sid, session_name) pair.

        Always creates a new PTY — each WebSocket connection gets its own
        tmux attach-session process.
        """
        key = (sid, session_name)
        # Clean up existing if present (e.g., double-join)
        existing = self._sessions.pop(key, None)
        if existing:
            await existing.cleanup()

        pty_session = PtySession(session_name)
        pty_session.start(cols, rows)
        self._sessions[key] = pty_session
        return pty_session

    def get(self, sid: str, session_name: str) -> PtySession | None:
        """Get existing PTY session or None."""
        return self._sessions.get((sid, session_name))

    async def remove(self, sid: str, session_name: str) -> None:
        """Remove and clean up a PTY session immediately."""
        session = self._sessions.pop((sid, session_name), None)
        if session:
            await session.cleanup()

    async def cleanup_all(self) -> None:
        """Clean up all PTY sessions concurrently. Called from app lifespan shutdown.

        Uses asyncio.gather so that N sessions clean up in ~3s (worst case)
        instead of N * 3s sequentially — prevents SIGKILL on shutdown.
        """
        if self._sessions:
            await asyncio.gather(
                *(s.cleanup() for s in self._sessions.values()),
                return_exceptions=True,
            )
        self._sessions.clear()
        log.info("All PTY sessions cleaned up")


def _record_pid(pid: int) -> None:
    """Append a PID to the tracking file."""
    append_pid(_PID_FILE, pid, logger=log)


def _unrecord_pid(pid: int) -> None:
    """Remove a PID from the tracking file."""
    remove_pid(_PID_FILE, pid, logger=log)
