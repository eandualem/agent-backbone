"""Tests for src/control_mode.py — tmux control mode streaming."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import ControlModeConfig
from src.control_mode import (
    ConnectionState,
    ControlModeConnection,
    ControlModeManager,
    ExitEvent,
    LayoutChangeEvent,
    ModeChangeEvent,
    OutputEvent,
    SessionEvent,
    WindowEvent,
    _unescape_output,
    parse_control_mode_line,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_config() -> ControlModeConfig:
    """ControlModeConfig with small values for fast testing."""
    return ControlModeConfig(
        buffer_size=100,
        reconnect_interval_seconds=1,
        max_reconnect_attempts=3,
        reconnect_backoff_factor=2.0,
    )


class MockStdout:
    """Simulates async subprocess stdout with pre-loaded lines."""

    def __init__(self, lines: list[bytes]):
        self._lines = iter(lines)

    async def readline(self) -> bytes:
        try:
            return next(self._lines)
        except StopIteration:
            return b""  # EOF


def _mock_proc(returncode: int | None = None, stdout: MockStdout | None = None) -> MagicMock:
    """Create a mock subprocess.Process with given properties."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345
    proc.stdout = stdout
    proc.wait = AsyncMock()
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# TestUnescapeOutput
# ---------------------------------------------------------------------------


class TestUnescapeOutput:
    """Tests for _unescape_output — tmux octal escape handling."""

    def test_plain_string_passes_through(self):
        """Plain text with no octal escapes is returned unchanged."""
        assert _unescape_output("hello world") == "hello world"

    def test_backslash_octal_134(self):
        """\\134 (octal for 92 = backslash) unescapes to a literal backslash."""
        assert _unescape_output("path\\134file") == "path\\file"

    def test_bel_octal_007(self):
        """\\007 (BEL character) unescapes correctly."""
        result = _unescape_output("alert\\007here")
        assert result == "alert\x07here"

    def test_multiple_escapes(self):
        """Multiple octal escapes in one string are all replaced."""
        result = _unescape_output("a\\134b\\007c\\012d")
        assert result == "a\\b\x07c\nd"

    def test_mixed_text_and_escapes(self):
        """Regular text surrounding octal escapes is preserved."""
        result = _unescape_output("start \\040 middle \\041 end")
        # \040 = space (32), \041 = ! (33)
        assert result == "start   middle ! end"


# ---------------------------------------------------------------------------
# TestParseControlModeLine
# ---------------------------------------------------------------------------


class TestParseControlModeLine:
    """Tests for parse_control_mode_line — event parsing from raw tmux lines."""

    def test_output_event(self):
        """%output %0 hello -> OutputEvent with unescaped data."""
        event = parse_control_mode_line("%output %0 hello")
        assert isinstance(event, OutputEvent)
        assert event.pane_id == "%0"
        assert event.data == "hello"

    def test_output_with_octal_escapes(self):
        """%output with octal escapes in data get unescaped."""
        event = parse_control_mode_line("%output %0 line\\012end")
        assert isinstance(event, OutputEvent)
        assert event.data == "line\nend"

    def test_output_preserves_spaces(self):
        """%output with spaces in data preserves them (split maxsplit=2)."""
        event = parse_control_mode_line("%output %0 hello world  foo")
        assert isinstance(event, OutputEvent)
        assert event.data == "hello world  foo"

    def test_pane_mode_changed(self):
        """%pane-mode-changed %0 -> ModeChangeEvent."""
        event = parse_control_mode_line("%pane-mode-changed %0")
        assert isinstance(event, ModeChangeEvent)
        assert event.pane_id == "%0"

    def test_layout_change(self):
        """%layout-change @0 abc -> LayoutChangeEvent."""
        event = parse_control_mode_line("%layout-change @0 abc")
        assert isinstance(event, LayoutChangeEvent)
        assert event.window_id == "@0"
        assert event.layout == "abc"

    def test_window_add(self):
        """%window-add @1 -> WindowEvent(action='add')."""
        event = parse_control_mode_line("%window-add @1")
        assert isinstance(event, WindowEvent)
        assert event.window_id == "@1"
        assert event.action == "add"

    def test_window_close(self):
        """%window-close @1 -> WindowEvent(action='close')."""
        event = parse_control_mode_line("%window-close @1")
        assert isinstance(event, WindowEvent)
        assert event.window_id == "@1"
        assert event.action == "close"

    def test_session_changed(self):
        """%session-changed $0 mysession -> SessionEvent."""
        event = parse_control_mode_line("%session-changed $0 mysession")
        assert isinstance(event, SessionEvent)
        assert event.session_id == "$0"
        assert event.name == "mysession"

    def test_exit_no_reason(self):
        """%exit with no reason -> ExitEvent(reason='')."""
        event = parse_control_mode_line("%exit")
        assert isinstance(event, ExitEvent)
        assert event.reason == ""

    def test_exit_with_reason(self):
        """%exit some reason -> ExitEvent(reason='some reason')."""
        event = parse_control_mode_line("%exit some reason")
        assert isinstance(event, ExitEvent)
        assert event.reason == "some reason"

    def test_markers_return_none(self):
        """%begin, %end, %error lines return None."""
        assert parse_control_mode_line("%begin 1234 0 0") is None
        assert parse_control_mode_line("%end 1234 0 0") is None
        assert parse_control_mode_line("%error 1234 0 0") is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert parse_control_mode_line("") is None

    def test_non_percent_line_returns_none(self):
        """Line not starting with % returns None."""
        assert parse_control_mode_line("some random text") is None


# ---------------------------------------------------------------------------
# TestControlModeConnectionLifecycle
# ---------------------------------------------------------------------------


class TestControlModeConnectionLifecycle:
    """Tests for ControlModeConnection start/stop lifecycle."""

    async def test_start_spawns_subprocess(self):
        """start() spawns subprocess with correct args and sets CONNECTED state."""
        config = _default_config()
        conn = ControlModeConnection("ike", config)

        mock_proc = _mock_proc(returncode=None)
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_exec:
            await conn.start()

            mock_exec.assert_called_once_with(
                "tmux",
                "-C",
                "attach-session",
                "-t",
                "ike",
                "-r",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        assert conn.state == ConnectionState.CONNECTED

    async def test_stop_sends_sigterm_and_disconnects(self):
        """stop() sends SIGTERM, waits, and sets state to DISCONNECTED."""
        config = _default_config()
        conn = ControlModeConnection("ike", config)

        mock_proc = _mock_proc(returncode=None)
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await conn.start()
            assert conn.state == ConnectionState.CONNECTED

            await conn.stop()

        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_stop_kills_after_timeout(self):
        """stop() sends SIGKILL after wait timeout."""
        config = _default_config()
        conn = ControlModeConnection("ike", config)

        mock_proc = _mock_proc(returncode=None)
        # Make wait_for raise TimeoutError to simulate slow shutdown
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await conn.start()

        with patch(
            "src.control_mode.asyncio.wait_for",
            new_callable=AsyncMock,
            side_effect=TimeoutError,
        ):
            await conn.stop()

        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        mock_proc.kill.assert_called_once()
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_state_transitions(self):
        """State: DISCONNECTED -> start() -> CONNECTED -> stop() -> DISCONNECTED."""
        config = _default_config()
        conn = ControlModeConnection("ike", config)
        assert conn.state == ConnectionState.DISCONNECTED

        mock_proc = _mock_proc(returncode=None)
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await conn.start()
            assert conn.state == ConnectionState.CONNECTED

            await conn.stop()
            assert conn.state == ConnectionState.DISCONNECTED

    async def test_start_noop_when_already_running(self):
        """start() is a no-op when the process is already running (returncode is None)."""
        config = _default_config()
        conn = ControlModeConnection("ike", config)

        mock_proc = _mock_proc(returncode=None)
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_exec:
            await conn.start()
            assert mock_exec.call_count == 1

            # Second start should be a no-op
            await conn.start()
            assert mock_exec.call_count == 1


# ---------------------------------------------------------------------------
# TestControlModeConnectionStream
# ---------------------------------------------------------------------------


class TestControlModeConnectionStream:
    """Tests for ControlModeConnection.stream() — async event generator."""

    async def test_stream_yields_parsed_events(self):
        """Stream yields parsed events from subprocess stdout."""
        config = _default_config()
        conn = ControlModeConnection("ike", config)

        stdout = MockStdout(
            [
                b"%output %0 hello\n",
                b"%pane-mode-changed %0\n",
                b"",  # EOF
            ]
        )
        mock_proc = _mock_proc(returncode=None, stdout=stdout)

        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await conn.start()

        async def passthrough_wait_for(coro, timeout):
            return await coro

        # Don't set stop_event — EOF triggers _reconnect which we mock to end the stream
        events = []
        with (
            patch("src.control_mode.asyncio.wait_for", side_effect=passthrough_wait_for),
            patch.object(conn, "_reconnect", new_callable=AsyncMock, return_value=False),
        ):
            async for event in conn.stream():
                events.append(event)

        assert len(events) == 2
        assert isinstance(events[0], OutputEvent)
        assert events[0].data == "hello"
        assert isinstance(events[1], ModeChangeEvent)

    async def test_stream_skips_none_results(self):
        """Stream skips None results (markers and empty lines)."""
        config = _default_config()
        conn = ControlModeConnection("ike", config)

        stdout = MockStdout(
            [
                b"%begin 1234 0 0\n",
                b"%output %0 data\n",
                b"%end 1234 0 0\n",
                b"\n",
                b"",  # EOF
            ]
        )
        mock_proc = _mock_proc(returncode=None, stdout=stdout)

        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await conn.start()

        async def passthrough_wait_for(coro, timeout):
            return await coro

        events = []
        with (
            patch("src.control_mode.asyncio.wait_for", side_effect=passthrough_wait_for),
            patch.object(conn, "_reconnect", new_callable=AsyncMock, return_value=False),
        ):
            async for event in conn.stream():
                events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], OutputEvent)
        assert events[0].data == "data"

    async def test_stream_ends_on_eof_when_stopped(self):
        """Stream ends on EOF when stop_event is set (intentional stop)."""
        config = _default_config()
        conn = ControlModeConnection("ike", config)

        stdout = MockStdout([b""])  # Immediate EOF
        mock_proc = _mock_proc(returncode=None, stdout=stdout)

        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await conn.start()

        conn._stop_event.set()

        async def passthrough_wait_for(coro, timeout):
            return await coro

        events = []
        with patch("src.control_mode.asyncio.wait_for", side_effect=passthrough_wait_for):
            async for event in conn.stream():
                events.append(event)

        assert events == []

    async def test_stream_reconnects_on_unexpected_eof(self):
        """Stream attempts reconnect on unexpected EOF (stop_event not set).

        Mock _reconnect to return False so stream ends cleanly.
        """
        config = _default_config()
        conn = ControlModeConnection("ike", config)

        stdout = MockStdout([b""])  # Immediate EOF
        mock_proc = _mock_proc(returncode=None, stdout=stdout)

        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await conn.start()

        # stop_event is NOT set — this is an unexpected disconnect

        async def passthrough_wait_for(coro, timeout):
            return await coro

        events = []
        with (
            patch("src.control_mode.asyncio.wait_for", side_effect=passthrough_wait_for),
            patch.object(
                conn, "_reconnect", new_callable=AsyncMock, return_value=False
            ) as mock_recon,
        ):
            async for event in conn.stream():
                events.append(event)

        assert events == []
        mock_recon.assert_called_once()


# ---------------------------------------------------------------------------
# TestReconnectionBackoff
# ---------------------------------------------------------------------------


class TestReconnectionBackoff:
    """Tests for ControlModeConnection._reconnect — exponential backoff."""

    async def test_backoff_delay_formula(self):
        """Backoff delay is interval * factor^(attempt-1)."""
        config = ControlModeConfig(
            buffer_size=100,
            reconnect_interval_seconds=2,
            max_reconnect_attempts=3,
            reconnect_backoff_factor=3.0,
        )
        conn = ControlModeConnection("ike", config)

        # Track the timeout values passed to wait_for (these are the sleep delays)
        recorded_delays: list[float] = []

        async def capture_wait_for(coro, timeout):
            recorded_delays.append(timeout)
            raise TimeoutError  # Simulate sleep completing

        # Make start() always fail so we go through all attempts
        with (
            patch(
                "src.control_mode.asyncio.wait_for",
                side_effect=capture_wait_for,
            ),
            patch.object(
                conn,
                "start",
                new_callable=AsyncMock,
                side_effect=Exception("connection failed"),
            ),
        ):
            result = await conn._reconnect()

        assert result is False
        assert conn.state == ConnectionState.FAILED

        # Verify backoff delays: interval * factor^(attempt-1)
        # Attempt 1: 2 * 3^0 = 2.0
        # Attempt 2: 2 * 3^1 = 6.0
        # Attempt 3: 2 * 3^2 = 18.0
        assert recorded_delays == [2.0, 6.0, 18.0]

    async def test_max_attempts_exhausted_becomes_failed(self):
        """Max attempts exhausted sets state to FAILED."""
        config = ControlModeConfig(
            buffer_size=100,
            reconnect_interval_seconds=1,
            max_reconnect_attempts=2,
            reconnect_backoff_factor=1.0,
        )
        conn = ControlModeConnection("ike", config)

        async def timeout_wait_for(coro, timeout):
            raise TimeoutError

        with (
            patch("src.control_mode.asyncio.wait_for", side_effect=timeout_wait_for),
            patch.object(
                conn,
                "start",
                new_callable=AsyncMock,
                side_effect=Exception("cannot connect"),
            ),
        ):
            result = await conn._reconnect()

        assert result is False
        assert conn.state == ConnectionState.FAILED

    async def test_stop_event_interrupts_reconnection(self):
        """stop_event set during sleep interrupts reconnection early."""
        config = ControlModeConfig(
            buffer_size=100,
            reconnect_interval_seconds=10,
            max_reconnect_attempts=5,
            reconnect_backoff_factor=2.0,
        )
        conn = ControlModeConnection("ike", config)

        async def event_fires_wait_for(coro, timeout):
            # Simulate the stop_event being set (wait_for returns normally)
            return None

        with patch("src.control_mode.asyncio.wait_for", side_effect=event_fires_wait_for):
            result = await conn._reconnect()

        # Should return False because stop_event.wait() returned (event was set)
        assert result is False
        # Should NOT reach FAILED — it was interrupted, not exhausted
        assert conn.state != ConnectionState.FAILED


# ---------------------------------------------------------------------------
# TestControlModeManager
# ---------------------------------------------------------------------------


class TestControlModeManager:
    """Tests for ControlModeManager — connection registry and lifecycle."""

    async def test_connect_creates_new_connection(self):
        """connect() creates a new connection and calls start()."""
        manager = ControlModeManager()
        config = _default_config()

        mock_proc = _mock_proc(returncode=None)
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            conn = await manager.connect("ike", config)

        assert conn is not None
        assert conn.session_name == "ike"
        assert conn.state == ConnectionState.CONNECTED

    async def test_connect_reuses_active_connection(self):
        """connect() reuses an existing active connection for the same session."""
        manager = ControlModeManager()
        config = _default_config()

        mock_proc = _mock_proc(returncode=None)
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            conn1 = await manager.connect("ike", config)
            conn2 = await manager.connect("ike", config)

        assert conn1 is conn2

    async def test_disconnect_stops_and_removes(self):
        """disconnect() stops the connection and removes it from the manager."""
        manager = ControlModeManager()
        config = _default_config()

        mock_proc = _mock_proc(returncode=None)
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await manager.connect("ike", config)

        await manager.disconnect("ike")
        assert manager.get_connection("ike") is None

    async def test_get_connection_returns_none_for_unknown(self):
        """get_connection() returns None for an unknown session."""
        manager = ControlModeManager()
        assert manager.get_connection("nonexistent") is None

    async def test_close_all_stops_all_connections(self):
        """close_all() stops all connections and clears the dict."""
        manager = ControlModeManager()
        config = _default_config()

        mock_proc = _mock_proc(returncode=None)
        with patch(
            "src.control_mode.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await manager.connect("ike", config)
            await manager.connect("leo", config)

        assert manager.get_connection("ike") is not None
        assert manager.get_connection("leo") is not None

        await manager.close_all()

        assert manager.get_connection("ike") is None
        assert manager.get_connection("leo") is None
