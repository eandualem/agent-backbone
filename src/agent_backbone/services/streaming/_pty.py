"""PTY-based terminal session management for interactive tmux access.

Spawns `tmux attach-session` in a PTY so that xterm.js receives the exact
same byte stream as a native terminal (Ghostty, iTerm). This gives correct
ANSI rendering, input echo via PTY line discipline, and proper SIGWINCH
resize propagation.

One PtySession per tmux session being viewed, with fan-out to multiple
browser clients via subscriber queues.
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

log = logging.getLogger(__name__)

# Max queued output chunks per subscriber before dropping oldest
_MAX_QUEUE_SIZE = 1000


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
        self._subscribers: dict[str, asyncio.Queue[str | None]] = {}
        # Dedicated thread pool so blocking os.read doesn't starve the default executor
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pty-{session_name}")
        # Incremental decoder handles multi-byte UTF-8 split across reads
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def start(self, cols: int = 80, rows: int = 24) -> None:
        """Spawn tmux attach-session in a PTY.

        Sets TERM=xterm-256color so tmux outputs xterm-dialect escape
        sequences (matching what xterm.js expects).
        """
        master_fd, slave_fd = os.openpty()

        # Set initial terminal size before spawning
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        env = {**os.environ, "TERM": "xterm-256color"}
        self._process = subprocess.Popen(
            ["tmux", "attach-session", "-t", self.session_name],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)
        self.master_fd = master_fd

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
        """Read from PTY master and broadcast to all subscribers."""
        loop = asyncio.get_event_loop()
        try:
            while True:
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
                for queue in list(self._subscribers.values()):
                    try:
                        queue.put_nowait(text)
                    except asyncio.QueueFull:
                        # Drop oldest for slow clients
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            queue.put_nowait(text)
                        except asyncio.QueueFull:
                            pass
        except asyncio.CancelledError:
            return
        finally:
            # Send sentinel to all subscribers
            for queue in list(self._subscribers.values()):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
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

    def subscribe(self, sid: str) -> asyncio.Queue[str | None]:
        """Add a subscriber. Returns their output queue."""
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._subscribers[sid] = queue
        log.info(
            "PTY subscriber added for '%s' (sid=%s, total=%d)",
            self.session_name,
            sid,
            len(self._subscribers),
        )
        return queue

    def unsubscribe(self, sid: str) -> None:
        """Remove a subscriber."""
        self._subscribers.pop(sid, None)
        log.info(
            "PTY subscriber removed for '%s' (sid=%s, remaining=%d)",
            self.session_name,
            sid,
            len(self._subscribers),
        )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def cleanup(self) -> None:
        """Close PTY and terminate tmux attach process."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()

        self._executor.shutdown(wait=False)

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

        if self._process is not None:
            try:
                self._process.send_signal(signal.SIGTERM)
                self._process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._process.kill()
                    self._process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._process = None

        log.info("PTY cleaned up for '%s'", self.session_name)


class PtyManager:
    """Manages PTY sessions — one per tmux session being viewed.

    Thread-safe via asyncio (single event loop). When the last subscriber
    leaves a session, the PTY is cleaned up.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, PtySession] = {}

    def get_or_create(self, session_name: str, cols: int = 80, rows: int = 24) -> PtySession:
        """Get existing or create new PTY session."""
        if session_name not in self._sessions:
            pty_session = PtySession(session_name)
            pty_session.start(cols, rows)
            self._sessions[session_name] = pty_session
        return self._sessions[session_name]

    def get(self, session_name: str) -> PtySession | None:
        """Get existing PTY session or None."""
        return self._sessions.get(session_name)

    def remove(self, session_name: str) -> None:
        """Remove and clean up a PTY session."""
        session = self._sessions.pop(session_name, None)
        if session:
            session.cleanup()

    def cleanup_all(self) -> None:
        """Clean up all PTY sessions. Called from app lifespan shutdown."""
        for session in self._sessions.values():
            session.cleanup()
        self._sessions.clear()
        log.info("All PTY sessions cleaned up")
