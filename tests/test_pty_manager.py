"""Tests for agent_backbone/services/streaming/_pty.py — 1:1 PTY model."""

from __future__ import annotations

import codecs
import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

from agent_backbone.services.streaming import PtyManager, PtySession

_PTY = "agent_backbone.services.streaming._pty"


class TestPtySession:
    def test_output_queue_exists(self):
        session = PtySession("test")
        assert session.output_queue is not None

    def test_output_queue_is_readonly_property(self):
        session = PtySession("test")
        queue = session.output_queue
        assert queue is session._output_queue

    def test_write_no_fd(self):
        """Write with no master_fd is a no-op."""
        session = PtySession("test")
        session.write("hello")  # No crash

    def test_resize_no_fd(self):
        """Resize with no master_fd is a no-op."""
        session = PtySession("test")
        session.resize(80, 24)  # No crash

    async def test_cleanup_no_process(self):
        """Cleanup with no process is safe."""
        session = PtySession("test")
        await session.cleanup()  # No crash

    async def test_start_and_cleanup(self):
        """Start spawns a tmux attach process and cleanup terminates it in executor."""
        with patch(f"{_PTY}.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            master_fd, slave_fd = os.openpty()
            with patch(f"{_PTY}.os.openpty", return_value=(master_fd, slave_fd)):
                with patch(f"{_PTY}.os.close"):
                    with patch(f"{_PTY}.asyncio.create_task"):
                        session = PtySession("test-session")
                        session.start(cols=120, rows=40)

                        assert session.master_fd == master_fd
                        assert session._process is mock_proc

                        # Verify tmux attach command
                        call_args = mock_popen.call_args
                        assert call_args[0][0] == ["tmux", "attach-session", "-t", "test-session"]
                        assert call_args[1]["env"]["TERM"] == "xterm-256color"

                        # Cleanup runs _terminate_process in executor
                        await session.cleanup()
                        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)

            # Clean up our test fds
            try:
                os.close(master_fd)
            except OSError:
                pass

    def test_write_sends_to_fd(self):
        """Write encodes and sends data to master fd."""
        session = PtySession("test")
        session.master_fd = 42

        with patch(f"{_PTY}.os.write") as mock_write:
            session.write("hello")
            mock_write.assert_called_once_with(42, b"hello")

    def test_resize_sends_ioctl(self):
        """Resize calls ioctl with TIOCSWINSZ."""
        session = PtySession("test")
        session.master_fd = 42

        with patch(f"{_PTY}.fcntl.ioctl") as mock_ioctl:
            session.resize(140, 35)
            mock_ioctl.assert_called_once()
            args = mock_ioctl.call_args[0]
            assert args[0] == 42  # fd

    def test_pause_clears_resume_event(self):
        """Pause sets flag and clears the resume event."""
        session = PtySession("test")
        assert session._resume_event.is_set()
        session.pause()
        assert session._paused is True
        assert not session._resume_event.is_set()

    def test_resume_sets_resume_event(self):
        """Resume clears flag and sets the resume event."""
        session = PtySession("test")
        session.pause()
        session.resume()
        assert session._paused is False
        assert session._resume_event.is_set()

    async def test_cleanup_kills_process_before_closing_fd(self):
        """Cleanup terminates process before closing the FD (no FD race)."""
        session = PtySession("test")
        session.master_fd = 42
        mock_proc = MagicMock()
        mock_proc.pid = 999
        session._process = mock_proc

        call_order: list[str] = []

        original_terminate = PtySession._terminate_process

        def track_terminate(proc):
            call_order.append("terminate")
            original_terminate(proc)

        def track_close(fd):
            call_order.append("close_fd")

        with (
            patch(f"{_PTY}.PtySession._terminate_process", side_effect=track_terminate),
            patch(f"{_PTY}.os.close", side_effect=track_close),
            patch(f"{_PTY}._unrecord_pid"),
        ):
            await session.cleanup()

        assert call_order.index("terminate") < call_order.index("close_fd")

    async def test_cleanup_executor_shutdown_waits(self):
        """Cleanup shuts down executor with wait=True after killing process."""
        session = PtySession("test")
        session._executor = MagicMock()

        with patch(f"{_PTY}._unrecord_pid"):
            await session.cleanup()

        session._executor.shutdown.assert_called_once_with(
            wait=True,
            cancel_futures=True,
        )

    def test_terminate_process_sigterm_then_wait(self):
        """_terminate_process sends SIGTERM and waits."""
        proc = MagicMock()
        PtySession._terminate_process(proc)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.wait.assert_called_once_with(timeout=2)

    def test_terminate_process_falls_back_to_kill(self):
        """_terminate_process falls back to kill on timeout."""
        proc = MagicMock()
        from subprocess import TimeoutExpired

        proc.wait.side_effect = [TimeoutExpired("cmd", 2), None]
        PtySession._terminate_process(proc)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once()


class TestPtySessionReadLoop:
    async def test_read_writes_to_output_queue(self):
        """Read loop writes output to the output queue."""
        session = PtySession("test")

        # Create a real pipe to simulate PTY
        read_fd, write_fd = os.pipe()
        session.master_fd = read_fd

        # Write some data
        os.write(write_fd, b"hello world")
        os.close(write_fd)  # Close write end so read returns EOF after data

        # Run the read loop directly
        await session._read_loop()

        # Output queue should have received the data
        data = session.output_queue.get_nowait()
        assert data == "hello world"

        # Should have received sentinel
        sentinel = session.output_queue.get_nowait()
        assert sentinel is None

        os.close(read_fd)

    async def test_read_handles_eof(self):
        """Read loop exits on EOF and sends sentinel."""
        session = PtySession("test")

        read_fd, write_fd = os.pipe()
        session.master_fd = read_fd
        os.close(write_fd)  # Immediate EOF

        await session._read_loop()

        sentinel = session.output_queue.get_nowait()
        assert sentinel is None

        os.close(read_fd)


class TestPtyManager:
    async def test_create_new(self):
        """Creates a new PTY session keyed by (sid, session_name)."""
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            session = await mgr.create("sid1", "test", 80, 24)
            assert session is not None
            assert session.session_name == "test"
            await session.cleanup()

    async def test_create_always_makes_new(self):
        """Each create call makes a new PTY (no reuse)."""
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            s1 = await mgr.create("sid1", "test")
            s2 = await mgr.create("sid2", "test")
            assert s1 is not s2
            await s1.cleanup()
            await s2.cleanup()

    async def test_create_cleans_up_existing(self):
        """Creating with same key cleans up existing PTY."""
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            s1 = await mgr.create("sid1", "test")
            s1.cleanup = AsyncMock()
            s2 = await mgr.create("sid1", "test")
            s1.cleanup.assert_awaited_once()
            assert s2 is not s1
            await s2.cleanup()

    async def test_get_existing(self):
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            await mgr.create("sid1", "test")
            assert mgr.get("sid1", "test") is not None
            assert mgr.get("sid2", "test") is None
            await mgr.cleanup_all()

    async def test_remove(self):
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            await mgr.create("sid1", "test")
            await mgr.remove("sid1", "test")
            assert mgr.get("sid1", "test") is None

    async def test_remove_nonexistent(self):
        mgr = PtyManager()
        await mgr.remove("sid1", "nope")  # No crash

    async def test_cleanup_all(self):
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            await mgr.create("sid1", "a")
            await mgr.create("sid2", "b")
            await mgr.cleanup_all()
            assert mgr.get("sid1", "a") is None
            assert mgr.get("sid2", "b") is None


class TestIncrementalDecoder:
    """Tests for the incremental UTF-8 decoder on PtySession (Gap 2, issue #344).

    PtySession uses codecs.getincrementaldecoder("utf-8")("replace") so that
    multi-byte UTF-8 characters split across consecutive os.read() calls are
    reassembled correctly instead of producing replacement characters.
    """

    def test_incremental_decoder_initialized(self):
        """A new PtySession has a _decoder that is a codecs.IncrementalDecoder."""
        session = PtySession("test")
        assert hasattr(session, "_decoder")
        assert isinstance(session._decoder, codecs.IncrementalDecoder)

    def test_split_utf8_decoded_correctly(self):
        """Splitting a multi-byte UTF-8 character across decoder calls reassembles it.

        The emoji U+1F389 (Party Popper) encodes as 4 bytes: b'\\xf0\\x9f\\x8e\\x89'.
        Feeding the first 2 bytes and then the remaining 2 bytes to the incremental
        decoder must produce the original emoji without any replacement characters.
        This simulates what _read_loop does when os.read returns partial sequences.
        """
        session = PtySession("test")
        emoji_bytes = "\U0001f389".encode()  # b'\xf0\x9f\x8e\x89'
        assert len(emoji_bytes) == 4

        # Simulate two separate os.read calls splitting the 4-byte sequence
        text1 = session._decoder.decode(emoji_bytes[:2])  # b'\xf0\x9f'
        text2 = session._decoder.decode(emoji_bytes[2:])  # b'\x8e\x89'

        combined = text1 + text2
        assert combined == "\U0001f389"
        # Ensure no replacement characters leaked through
        assert "\ufffd" not in combined

    async def test_split_utf8_via_read_loop(self):
        """The read loop correctly decodes a multi-byte char split across two reads.

        Uses a real pipe with two separate writes. Each os.write produces one
        os.read call (pipe semantics: reads return whatever is available, not
        waiting for a full buffer). The incremental decoder buffers the partial
        sequence from the first read and completes it on the second.
        """
        session = PtySession("test")

        read_fd, write_fd = os.pipe()
        session.master_fd = read_fd

        emoji_bytes = "\U0001f389".encode()  # 4 bytes

        # Write first half, then second half, then close.
        # Each write is a separate chunk in the pipe buffer.
        os.write(write_fd, emoji_bytes[:2])
        os.write(write_fd, emoji_bytes[2:])
        os.close(write_fd)

        await session._read_loop()

        # Collect all text chunks from the queue (before the sentinel)
        chunks: list[str] = []
        while True:
            item = session.output_queue.get_nowait()
            if item is None:
                break
            chunks.append(item)

        combined = "".join(chunks)
        # The emoji must appear intact regardless of how many chunks arrived
        assert "\U0001f389" in combined
        # No replacement characters
        assert "\ufffd" not in combined

        os.close(read_fd)
