"""PTY-based terminal session management for interactive tmux access.

Spawns ``tmux attach-session`` in a PTY so a browser terminal receives the
exact byte stream a native terminal would (faithful ANSI rendering, proper
SIGWINCH resize propagation). Output only: nothing is ever written to the
PTY, so remote viewers cannot type into an agent.

One PtySession per Socket.IO connection; tmux does the multiplexing.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import logging
import os
import signal
import struct
import subprocess
import termios
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 1000
"""Queued output chunks per viewer before the oldest are dropped."""


class PtySession:
    """A PTY running ``tmux attach-session``, read-only from our side."""

    def __init__(self, session_name: str, pid_file: Path | None = None) -> None:
        self.session_name = session_name
        self._pid_file = pid_file
        self.master_fd: int | None = None
        self.tty_name: str | None = None
        self._process: subprocess.Popen | None = None
        self._reader_task: asyncio.Task | None = None
        # Single output queue for the 1:1 connection
        self._output_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        # Cleared while the viewer asks for backpressure; the read loop waits on it
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
            try:
                self.tty_name = os.ttyname(slave_fd)
            except OSError:
                self.tty_name = None

            env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
            env.pop("TMUX", None)  # the backbone itself may run inside tmux
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

        _record_pid(self._pid_file, self._process.pid)

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
                    # Slow viewer: drop the oldest chunk and tell the client
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._output_queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        self._output_queue.put_nowait(text)
                    if self.on_data_dropped is not None:
                        with contextlib.suppress(Exception):
                            self.on_data_dropped(self.session_name)
        except asyncio.CancelledError:
            return
        finally:
            # Guarantee sentinel delivery — drain one slot if needed
            try:
                self._output_queue.put_nowait(None)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._output_queue.get_nowait()
                try:
                    self._output_queue.put_nowait(None)
                except asyncio.QueueFull:
                    log.error(
                        "Cannot deliver PTY sentinel for '%s' — forwarding task may leak",
                        self.session_name,
                    )
            log.info("PTY read loop ended for '%s'", self.session_name)

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
        """Stop reading from the PTY (viewer backpressure)."""
        self._resume_event.clear()

    def resume(self) -> None:
        """Resume reading from the PTY."""
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
            _unrecord_pid(self._pid_file, pid)

        # Now safe to wait for the executor — the read thread should have exited
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._executor.shutdown(wait=True, cancel_futures=True),
        )

        # FD is safe to close — no threads reading it
        if self.master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
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
    """PTY sessions, one per (viewer sid, tmux session) pair.

    ``pid_file`` records the attach processes this backbone spawned so a
    restart can kill orphans without touching the user's own tmux clients.
    """

    def __init__(self, pid_file: Path | None = None) -> None:
        self._sessions: dict[tuple[str, str], PtySession] = {}
        self._pid_file = pid_file
        self._cleanup_orphaned_processes()

    def _cleanup_orphaned_processes(self) -> None:
        pid_file = self._pid_file
        try:
            if pid_file is None or not pid_file.exists():
                return
            content = pid_file.read_text().strip()
            if not content:
                return
            pids = [int(p) for p in content.split("\n") if p.strip()]
            killed = 0
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                    log.info("Killed orphaned tmux attach-session (pid=%d)", pid)
                except OSError:
                    pass  # Already dead
            if killed:
                log.info("Cleaned up %d orphaned PTY process(es)", killed)
            pid_file.write_text("")
        except Exception:
            log.warning("Failed to clean up orphaned PTY processes", exc_info=True)

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

        pty_session = PtySession(session_name, self._pid_file)
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


def _record_pid(pid_file: Path | None, pid: int) -> None:
    if pid_file is None:
        return
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        with pid_file.open("a") as f:
            f.write(f"{pid}\n")
    except OSError:
        log.debug("Failed to record PID %d", pid)


def _unrecord_pid(pid_file: Path | None, pid: int) -> None:
    if pid_file is None:
        return
    try:
        if not pid_file.exists():
            return
        lines = pid_file.read_text().strip().split("\n")
        remaining = [ln for ln in lines if ln.strip() and ln.strip() != str(pid)]
        pid_file.write_text("\n".join(remaining) + "\n" if remaining else "")
    except OSError:
        log.debug("Failed to unrecord PID %d", pid)
