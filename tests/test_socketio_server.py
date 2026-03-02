"""Tests for api/socketio_server.py — Socket.IO terminal namespace (PTY-based)."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

from api.socketio_server import (
    INPUT_RATE_LIMIT,
    MAX_COLS,
    MAX_INPUT_BYTES,
    MAX_ROWS,
    MIN_COLS,
    MIN_ROWS,
    TerminalNamespace,
)


def _make_namespace() -> TerminalNamespace:
    """Create a TerminalNamespace with a mocked server."""
    ns = TerminalNamespace("/terminal")
    ns.server = MagicMock()
    ns.emit = AsyncMock()
    ns.enter_room = AsyncMock()
    ns.leave_room = AsyncMock()
    return ns


def _mock_pty_session():
    """Create a mock PtySession."""
    session = MagicMock()
    session.subscriber_count = 1
    session.subscribe = MagicMock(return_value=asyncio.Queue())
    session.unsubscribe = MagicMock()
    session.write = MagicMock()
    session.resize = MagicMock()
    session.cleanup = MagicMock()
    return session


def _mock_pty_manager(pty_session=None):
    """Create a mock PtyManager."""
    mgr = MagicMock()
    mgr.get_or_create = MagicMock(return_value=pty_session)
    mgr.get = MagicMock(return_value=pty_session)
    mgr.remove = MagicMock()
    return mgr


class TestOnConnect:
    async def test_connect_allowed_dev_mode(self):
        """Connections allowed when BACKBONE_API_KEY is not set."""
        ns = _make_namespace()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BACKBONE_API_KEY", None)
            result = await ns.on_connect("sid1", {}, auth=None)
            assert result is True

    async def test_connect_valid_key(self):
        """Connections allowed with matching API key."""
        ns = _make_namespace()
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret-key"}):
            result = await ns.on_connect("sid1", {}, auth={"api_key": "secret-key"})
            assert result is True

    async def test_connect_invalid_key(self):
        """Connections rejected with wrong API key."""
        ns = _make_namespace()
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret-key"}):
            result = await ns.on_connect("sid1", {}, auth={"api_key": "wrong-key"})
            assert result is False

    async def test_connect_missing_auth(self):
        """Connections rejected when auth dict is missing."""
        ns = _make_namespace()
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret-key"}):
            result = await ns.on_connect("sid1", {}, auth=None)
            assert result is False

    async def test_connect_empty_auth(self):
        """Connections rejected when auth dict has no api_key."""
        ns = _make_namespace()
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret-key"}):
            result = await ns.on_connect("sid1", {}, auth={})
            assert result is False


class TestOnJoin:
    async def test_join_missing_session_name(self):
        """Emits error when session name is empty."""
        ns = _make_namespace()
        await ns.on_join("sid1", {})
        ns.emit.assert_awaited_once_with("error", {"message": "Missing session name"}, to="sid1")

    async def test_join_nonexistent_session(self):
        """Emits error when tmux session doesn't exist."""
        ns = _make_namespace()
        with patch("api.socketio_server.session_exists", new_callable=AsyncMock) as mock_exists:
            mock_exists.return_value = False
            await ns.on_join("sid1", {"session": "ghost"})
            ns.emit.assert_awaited_once()
            assert "not found" in ns.emit.call_args[0][1]["message"]

    async def test_join_success(self):
        """Successful join without client dims: uses pane dimensions, resizes PTY."""
        ns = _make_namespace()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)  # Sentinel so forwarding task completes

        pty_session = _mock_pty_session()
        pty_session.subscribe.return_value = queue
        mgr = _mock_pty_manager(pty_session)

        mock_pane = {
            "pane_id": "%0",
            "pane_index": "0",
            "pane_width": "120",
            "pane_height": "40",
            "pane_active": True,
        }

        with (
            patch("api.socketio_server.session_exists", new_callable=AsyncMock, return_value=True),
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch("api.socketio_server.capture_pane", new_callable=AsyncMock) as mock_capture,
            patch("api.socketio_server.list_panes", new_callable=AsyncMock) as mock_panes,
            patch(
                "api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True
            ) as mock_resize_win,
            patch(
                "api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            mock_capture.return_value = "$ hello world\n"
            mock_panes.return_value = [mock_pane]
            await ns.on_join("sid1", {"session": "ike"})

            # Should have created/gotten PTY with pane dimensions
            mgr.get_or_create.assert_called_once_with("ike", 120, 40)

            # Should have explicitly resized PTY
            pty_session.resize.assert_called_once_with(120, 40)

            # Should have called tmux resize-window
            mock_resize_win.assert_awaited_once_with("ike", 120, 40)

            # Should have entered room
            ns.enter_room.assert_called_once_with("sid1", "session:ike")

            # Should have emitted snapshot with dimensions
            snapshot_calls = [c for c in ns.emit.call_args_list if c[0][0] == "snapshot"]
            assert len(snapshot_calls) == 1
            payload = snapshot_calls[0][0][1]
            assert payload["session"] == "ike"
            assert payload["data"] == "$ hello world\n"
            assert payload["cols"] == 120
            assert payload["rows"] == 40

            # Should be tracked
            assert "ike" in ns._subscriptions["sid1"]

            # Let forwarding task complete
            await asyncio.sleep(0.15)

    async def test_join_with_client_dimensions(self):
        """Join with client-provided cols/rows uses those instead of pane dims."""
        ns = _make_namespace()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        pty_session = _mock_pty_session()
        pty_session.subscribe.return_value = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch("api.socketio_server.session_exists", new_callable=AsyncMock, return_value=True),
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch("api.socketio_server.capture_pane", new_callable=AsyncMock) as mock_capture,
            patch("api.socketio_server.list_panes", new_callable=AsyncMock) as mock_panes,
            patch(
                "api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True
            ) as mock_resize_win,
            patch(
                "api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            mock_capture.return_value = "$ hello\n"
            # list_panes should NOT be called when client provides dimensions
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})

            mock_panes.assert_not_called()
            mgr.get_or_create.assert_called_once_with("ike", 160, 35)
            pty_session.resize.assert_called_once_with(160, 35)
            mock_resize_win.assert_awaited_once_with("ike", 160, 35)

            snapshot_calls = [c for c in ns.emit.call_args_list if c[0][0] == "snapshot"]
            payload = snapshot_calls[0][0][1]
            assert payload["cols"] == 160
            assert payload["rows"] == 35

            await asyncio.sleep(0.15)

    async def test_join_client_dimensions_clamped(self):
        """Client-provided dimensions are clamped to MIN/MAX bounds."""
        ns = _make_namespace()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        pty_session = _mock_pty_session()
        pty_session.subscribe.return_value = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch("api.socketio_server.session_exists", new_callable=AsyncMock, return_value=True),
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch("api.socketio_server.capture_pane", new_callable=AsyncMock, return_value=""),
            patch(
                "api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True
            ) as mock_resize_win,
            patch(
                "api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 9999, "rows": 0})

            mgr.get_or_create.assert_called_once_with("ike", MAX_COLS, MIN_ROWS)
            pty_session.resize.assert_called_once_with(MAX_COLS, MIN_ROWS)
            mock_resize_win.assert_awaited_once_with("ike", MAX_COLS, MIN_ROWS)

            await asyncio.sleep(0.15)


class TestOnInput:
    async def test_input_not_joined(self):
        """Emits error when not joined to the session."""
        ns = _make_namespace()
        await ns.on_input("sid1", {"session": "ike", "data": "ls\r"})
        ns.emit.assert_awaited_once()
        assert "Not joined" in ns.emit.call_args[0][1]["message"]

    async def test_input_empty_data(self):
        """Silently ignores empty input data."""
        ns = _make_namespace()
        await ns.on_input("sid1", {"session": "ike", "data": ""})
        ns.emit.assert_not_awaited()

    async def test_input_writes_to_pty(self):
        """Writes input directly to PTY master fd."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_input("sid1", {"session": "ike", "data": "ls\r"})
            pty_session.write.assert_called_once_with("ls\r")
            ns.emit.assert_not_awaited()


class TestOnResize:
    async def test_resize_not_joined(self):
        """Emits error when not joined to the session."""
        ns = _make_namespace()
        await ns.on_resize("sid1", {"session": "ike", "cols": 140, "rows": 35})
        ns.emit.assert_awaited_once()
        assert "Not joined" in ns.emit.call_args[0][1]["message"]

    async def test_resize_missing_fields(self):
        """Silently ignores resize with missing cols/rows."""
        ns = _make_namespace()
        await ns.on_resize("sid1", {"session": "ike"})
        ns.emit.assert_not_awaited()

    async def test_resize_calls_pty_and_tmux_resize(self):
        """Calls PTY resize with TIOCSWINSZ and tmux resize-window."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with (
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True
            ) as mock_resize_win,
        ):
            await ns.on_resize("sid1", {"session": "ike", "cols": 140, "rows": 35})
            pty_session.resize.assert_called_once_with(140, 35)
            mock_resize_win.assert_awaited_once_with("ike", 140, 35)
            ns.emit.assert_not_awaited()


class TestOnLeave:
    async def test_leave_not_subscribed(self):
        """Leave with no active subscription is a no-op."""
        ns = _make_namespace()
        await ns.on_leave("sid1", {"session": "ike"})

    async def test_leave_cleans_up_and_removes_pty(self):
        """Leave unsubscribes and removes PTY if last subscriber."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        pty_session.subscriber_count = 0  # Will be last subscriber after unsubscribe
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task = asyncio.create_task(_block())
        ns._subscriptions["sid1"] = {"ike": task}

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_leave("sid1", {"session": "ike"})

        assert task.cancelled()
        pty_session.unsubscribe.assert_called_once_with("sid1")
        mgr.remove.assert_called_once_with("ike")  # Last subscriber → remove PTY
        ns.leave_room.assert_called_once_with("sid1", "session:ike")

    async def test_leave_keeps_pty_with_remaining_subscribers(self):
        """Leave keeps PTY alive when other subscribers remain."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        pty_session.subscriber_count = 1  # Still has subscribers after this one leaves
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task = asyncio.create_task(_block())
        ns._subscriptions["sid1"] = {"ike": task}

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_leave("sid1", {"session": "ike"})

        pty_session.unsubscribe.assert_called_once_with("sid1")
        mgr.remove.assert_not_called()  # PTY kept alive


class TestOnDisconnect:
    async def test_disconnect_cleans_up_all_sessions(self):
        """Disconnect cleans up all session subscriptions."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        pty_session.subscriber_count = 0
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task1 = asyncio.create_task(_block())
        task2 = asyncio.create_task(_block())

        ns._subscriptions["sid1"] = {
            "ike": task1,
            "leo": task2,
        }

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_disconnect("sid1")

        assert pty_session.unsubscribe.call_count == 2
        assert "sid1" not in ns._subscriptions


class TestForwardPtyOutput:
    async def test_forward_emits_raw_data(self):
        """Forwarding task emits raw PTY output as terminal_output events."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        await queue.put("hello world")
        await queue.put("\x1b[32mgreen text\x1b[0m")
        await queue.put(None)

        await ns._forward_pty_output("sid1", "ike", queue)

        assert ns.emit.await_count == 2

        first = ns.emit.call_args_list[0]
        assert first[0][0] == "terminal_output"
        assert first[0][1] == {"session": "ike", "data": "hello world"}

        second = ns.emit.call_args_list[1]
        assert second[0][1] == {"session": "ike", "data": "\x1b[32mgreen text\x1b[0m"}

    async def test_forward_stops_on_sentinel(self):
        """Forwarding task exits on None sentinel."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        await ns._forward_pty_output("sid1", "ike", queue)
        ns.emit.assert_not_awaited()

    async def test_forward_handles_cancellation(self):
        """Forwarding task exits cleanly on cancellation."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        task = asyncio.create_task(ns._forward_pty_output("sid1", "ike", queue))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestInputValidation:
    """Gap 1 — Input size validation in on_input."""

    async def test_input_truncated_when_too_large(self):
        """Input exceeding MAX_INPUT_BYTES is truncated before writing to PTY."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        oversized_data = "A" * (MAX_INPUT_BYTES + 500)

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_input("sid1", {"session": "ike", "data": oversized_data})

            pty_session.write.assert_called_once()
            written_data = pty_session.write.call_args[0][0]
            assert len(written_data) == MAX_INPUT_BYTES
            assert written_data == "A" * MAX_INPUT_BYTES

    async def test_input_at_limit_passes_through(self):
        """Input exactly at MAX_INPUT_BYTES passes through without truncation."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        exact_data = "B" * MAX_INPUT_BYTES

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_input("sid1", {"session": "ike", "data": exact_data})

            pty_session.write.assert_called_once_with(exact_data)


class TestResizeBounds:
    """Gap 3 — Resize bounds validation in on_resize."""

    async def test_resize_clamps_large_values(self):
        """Cols and rows exceeding maximums are clamped to MAX_COLS/MAX_ROWS."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_resize("sid1", {"session": "ike", "cols": 10000, "rows": 10000})
            pty_session.resize.assert_called_once_with(MAX_COLS, MAX_ROWS)

    async def test_resize_clamps_small_values(self):
        """Cols and rows below minimums are clamped to MIN_COLS/MIN_ROWS."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_resize("sid1", {"session": "ike", "cols": 1, "rows": 1})
            pty_session.resize.assert_called_once_with(MIN_COLS, MIN_ROWS)

    async def test_resize_invalid_values(self):
        """Non-numeric cols/rows cause silent return without calling PTY resize."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_resize("sid1", {"session": "ike", "cols": "abc", "rows": "xyz"})
            pty_session.resize.assert_not_called()
            ns.emit.assert_not_awaited()


class TestRateLimiting:
    """Gap 4 — Rate limiting in on_input."""

    async def test_rate_limit_allows_normal_traffic(self):
        """Input events within the rate limit are all forwarded to PTY."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            for _ in range(50):
                await ns.on_input("sid1", {"session": "ike", "data": "x"})

            assert pty_session.write.call_count == 50

    async def test_rate_limit_blocks_excessive_traffic(self):
        """Input events exceeding the rate limit are dropped."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            for _ in range(150):
                await ns.on_input("sid1", {"session": "ike", "data": "x"})

            # First INPUT_RATE_LIMIT calls pass, the rest are dropped
            assert pty_session.write.call_count == INPUT_RATE_LIMIT

    async def test_rate_limit_cleanup_on_leave(self):
        """Leaving a session removes the rate-limit timestamps for that sid+session."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        pty_session.subscriber_count = 0
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task = asyncio.create_task(_block())
        ns._subscriptions["sid1"] = {"ike": task}

        # Seed rate-limit timestamps
        ns._input_timestamps[("sid1", "ike")] = [1.0, 2.0, 3.0]

        with patch("api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_leave("sid1", {"session": "ike"})

        assert ("sid1", "ike") not in ns._input_timestamps


class TestOutputBackpressure:
    """Gap 5 — Backpressure handling in _forward_pty_output."""

    async def test_forward_continues_after_emit_failure(self):
        """Output forwarding continues when emit raises on one message."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        await queue.put("message1")
        await queue.put("message2")
        await queue.put(None)

        # First emit raises, second succeeds
        ns.emit = AsyncMock(side_effect=[Exception("slow client"), None])

        await ns._forward_pty_output("sid1", "ike", queue)

        # Both messages were attempted (emit called twice)
        assert ns.emit.await_count == 2

        # Second call got the second message
        second_call = ns.emit.call_args_list[1]
        assert second_call[0][0] == "terminal_output"
        assert second_call[0][1] == {"session": "ike", "data": "message2"}


class TestDimensionTracking:
    """Per-client dimension tracking, original dims save/restore, min-dims strategy."""

    async def _join_client(self, ns, sid, session_name, cols, rows, mgr, pty_session):
        """Helper: join a client with given dimensions, mocking all dependencies."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.subscribe.return_value = queue

        with (
            patch("api.socketio_server.session_exists", new_callable=AsyncMock, return_value=True),
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch("api.socketio_server.capture_pane", new_callable=AsyncMock, return_value=""),
            patch("api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True),
            patch(
                "api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join(sid, {"session": session_name, "cols": cols, "rows": rows})
            await asyncio.sleep(0.1)

    async def test_join_saves_original_dims(self):
        """First join queries and stores original tmux window dimensions."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)

        assert ns._original_dims["ike"] == (200, 50)
        assert ns._client_dims["ike"]["sid1"] == (160, 35)

    async def test_join_second_client_no_re_query(self):
        """Second join to same session does not re-query original dims."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)
        assert ns._original_dims["ike"] == (200, 50)

        # Manually set original_dims to a different value to prove second join doesn't overwrite
        ns._original_dims["ike"] = (200, 50)

        await self._join_client(ns, "sid2", "ike", 140, 30, mgr, pty_session)

        # Original dims should still be from first join
        assert ns._original_dims["ike"] == (200, 50)
        # Both clients should be tracked
        assert "sid1" in ns._client_dims["ike"]
        assert "sid2" in ns._client_dims["ike"]

    async def test_multi_client_uses_minimum_dims(self):
        """Two clients at different sizes uses min(cols) x min(rows)."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)

        # Second client with smaller dimensions — should trigger min-dims
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.subscribe.return_value = queue
        pty_session.resize.reset_mock()

        with (
            patch("api.socketio_server.session_exists", new_callable=AsyncMock, return_value=True),
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch("api.socketio_server.capture_pane", new_callable=AsyncMock, return_value=""),
            patch(
                "api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True
            ) as mock_resize_win,
            patch(
                "api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid2", {"session": "ike", "cols": 140, "rows": 30})

            # Effective dims should be min(160,140) x min(35,30) = 140 x 30
            pty_session.resize.assert_called_once_with(140, 30)
            mock_resize_win.assert_awaited_once_with("ike", 140, 30)

            await asyncio.sleep(0.1)

    async def test_resize_updates_client_dims(self):
        """Resize event updates tracked dims and recomputes effective dimensions."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)
        await self._join_client(ns, "sid2", "ike", 140, 30, mgr, pty_session)

        pty_session.resize.reset_mock()

        # sid1 resizes smaller
        with (
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True
            ) as mock_resize_win,
        ):
            await ns.on_resize("sid1", {"session": "ike", "cols": 120, "rows": 25})

            # Updated client dims
            assert ns._client_dims["ike"]["sid1"] == (120, 25)

            # Effective should be min(120,140) x min(25,30) = 120 x 25
            pty_session.resize.assert_called_once_with(120, 25)
            mock_resize_win.assert_awaited_once_with("ike", 120, 25)

    async def test_cleanup_last_client_restores_dims(self):
        """Last client disconnect calls resize_window with original dims."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        pty_session.subscriber_count = 0
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)

        with (
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True
            ) as mock_resize_win,
        ):
            await ns.on_leave("sid1", {"session": "ike"})

            # Should restore original dims (200, 50)
            mock_resize_win.assert_awaited_once_with("ike", 200, 50)

        # Tracking state should be cleaned up
        assert "ike" not in ns._client_dims
        assert "ike" not in ns._original_dims

    async def test_cleanup_not_last_recomputes_effective(self):
        """When one of two clients leaves, recomputes min dims for remaining."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        pty_session.subscriber_count = 1  # Still has subscribers after leave
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)
        await self._join_client(ns, "sid2", "ike", 140, 30, mgr, pty_session)

        pty_session.resize.reset_mock()

        with (
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True
            ) as mock_resize_win,
        ):
            # sid2 (the smaller one) leaves — remaining sid1 should resize up
            await ns.on_leave("sid2", {"session": "ike"})

            # Should resize to sid1's dims (160, 35) — now the only client
            mock_resize_win.assert_awaited_once_with("ike", 160, 35)
            pty_session.resize.assert_called_once_with(160, 35)

        # sid2 should be removed, sid1 should remain
        assert "sid2" not in ns._client_dims.get("ike", {})
        assert ns._client_dims["ike"]["sid1"] == (160, 35)
        # Original dims should still be tracked (not last client)
        assert "ike" in ns._original_dims

    async def test_cleanup_clears_dim_tracking(self):
        """Full disconnect clears all dimension tracking for the client."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        pty_session.subscriber_count = 0
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)

        with (
            patch("api.socketio_server.get_pty_manager", return_value=mgr),
            patch("api.socketio_server.resize_window", new_callable=AsyncMock, return_value=True),
        ):
            await ns.on_disconnect("sid1")

        assert "ike" not in ns._client_dims
        assert "ike" not in ns._original_dims
        assert "sid1" not in ns._subscriptions
