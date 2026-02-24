"""Tests for src/pty_manager.py."""

from __future__ import annotations

import codecs
import os
import signal
from unittest.mock import MagicMock, patch

from src.pty_manager import PtyManager, PtySession


class TestPtySession:
    def test_subscribe_returns_queue(self):
        session = PtySession("test")
        queue = session.subscribe("sid1")
        assert queue is not None
        assert session.subscriber_count == 1

    def test_multiple_subscribers(self):
        session = PtySession("test")
        session.subscribe("sid1")
        session.subscribe("sid2")
        assert session.subscriber_count == 2

    def test_unsubscribe(self):
        session = PtySession("test")
        session.subscribe("sid1")
        session.subscribe("sid2")
        session.unsubscribe("sid1")
        assert session.subscriber_count == 1

    def test_unsubscribe_nonexistent(self):
        session = PtySession("test")
        session.unsubscribe("nope")  # No error
        assert session.subscriber_count == 0

    def test_write_no_fd(self):
        """Write with no master_fd is a no-op."""
        session = PtySession("test")
        session.write("hello")  # No crash

    def test_resize_no_fd(self):
        """Resize with no master_fd is a no-op."""
        session = PtySession("test")
        session.resize(80, 24)  # No crash

    def test_cleanup_no_process(self):
        """Cleanup with no process is safe."""
        session = PtySession("test")
        session.cleanup()  # No crash

    def test_start_and_cleanup(self):
        """Start spawns a tmux attach process and cleanup kills it."""
        with patch("src.pty_manager.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            master_fd, slave_fd = os.openpty()
            with patch("src.pty_manager.os.openpty", return_value=(master_fd, slave_fd)):
                with patch("src.pty_manager.os.close"):
                    with patch("src.pty_manager.asyncio.create_task"):
                        session = PtySession("test-session")
                        session.start(cols=120, rows=40)

                        assert session.master_fd == master_fd
                        assert session._process is mock_proc

                        # Verify tmux attach command
                        call_args = mock_popen.call_args
                        assert call_args[0][0] == [
                            "tmux", "attach-session", "-t", "test-session"
                        ]
                        assert call_args[1]["env"]["TERM"] == "xterm-256color"

                        # Cleanup
                        session.cleanup()
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

        with patch("src.pty_manager.os.write") as mock_write:
            session.write("hello")
            mock_write.assert_called_once_with(42, b"hello")

    def test_resize_sends_ioctl(self):
        """Resize calls ioctl with TIOCSWINSZ."""
        session = PtySession("test")
        session.master_fd = 42

        with patch("src.pty_manager.fcntl.ioctl") as mock_ioctl:
            session.resize(140, 35)
            mock_ioctl.assert_called_once()
            args = mock_ioctl.call_args[0]
            assert args[0] == 42  # fd


class TestPtySessionReadLoop:
    async def test_read_broadcasts_to_subscribers(self):
        """Read loop broadcasts output to all subscribers."""
        session = PtySession("test")
        q1 = session.subscribe("sid1")
        q2 = session.subscribe("sid2")

        # Create a real pipe to simulate PTY
        read_fd, write_fd = os.pipe()
        session.master_fd = read_fd

        # Write some data
        os.write(write_fd, b"hello world")
        os.close(write_fd)  # Close write end so read returns EOF after data

        # Run the read loop directly
        await session._read_loop()

        # Both subscribers should have received the data
        data1 = q1.get_nowait()
        assert data1 == "hello world"

        data2 = q2.get_nowait()
        assert data2 == "hello world"

        # Both should have received sentinel
        sentinel1 = q1.get_nowait()
        assert sentinel1 is None

        os.close(read_fd)

    async def test_read_handles_eof(self):
        """Read loop exits on EOF and sends sentinels."""
        session = PtySession("test")
        q = session.subscribe("sid1")

        read_fd, write_fd = os.pipe()
        session.master_fd = read_fd
        os.close(write_fd)  # Immediate EOF

        await session._read_loop()

        sentinel = q.get_nowait()
        assert sentinel is None

        os.close(read_fd)


class TestPtyManager:
    def test_get_or_create_new(self):
        """Creates a new PTY session."""
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            session = mgr.get_or_create("test", 80, 24)
            assert session is not None
            assert session.session_name == "test"
            session.cleanup()

    def test_get_or_create_reuses(self):
        """Reuses existing PTY session."""
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            s1 = mgr.get_or_create("test")
            s2 = mgr.get_or_create("test")
            assert s1 is s2
            s1.cleanup()

    def test_get_existing(self):
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            mgr.get_or_create("test")
            assert mgr.get("test") is not None
            assert mgr.get("nonexistent") is None
            mgr.cleanup_all()

    def test_remove(self):
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            mgr.get_or_create("test")
            mgr.remove("test")
            assert mgr.get("test") is None

    def test_remove_nonexistent(self):
        mgr = PtyManager()
        mgr.remove("nope")  # No crash

    def test_cleanup_all(self):
        mgr = PtyManager()
        with patch.object(PtySession, "start"):
            mgr.get_or_create("a")
            mgr.get_or_create("b")
            mgr.cleanup_all()
            assert mgr.get("a") is None
            assert mgr.get("b") is None


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
        q = session.subscribe("sid1")

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
            item = q.get_nowait()
            if item is None:
                break
            chunks.append(item)

        combined = "".join(chunks)
        # The emoji must appear intact regardless of how many chunks arrived
        assert "\U0001f389" in combined
        # No replacement characters
        assert "\ufffd" not in combined

        os.close(read_fd)
