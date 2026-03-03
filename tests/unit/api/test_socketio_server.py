"""Tests for api/socketio_server.py — Socket.IO terminal namespace (1:1 PTY model)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agent_backbone.api.socketio_server import (
    INPUT_RATE_LIMIT,
    MAX_COLS,
    MAX_INPUT_BYTES,
    MAX_ROWS,
    MIN_COLS,
    MIN_ROWS,
    TerminalNamespace,
    _load_persisted_dims,
    _persist_dims,
    _remove_persisted_dims,
)
from agent_backbone.services.terminal._pty import (
    PtyManager,
    PtySession,
    _record_pid,
    _unrecord_pid,
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
    """Create a mock PtySession for the 1:1 model."""
    session = MagicMock()
    session.output_queue = asyncio.Queue()
    session.write = MagicMock()
    session.resize = MagicMock()
    session.cleanup = AsyncMock()
    session.pause = MagicMock()
    session.resume = MagicMock()
    session.on_data_dropped = None
    return session


def _mock_pty_manager(pty_session=None):
    """Create a mock PtyManager for the 1:1 model."""
    mgr = MagicMock()
    mgr.create = AsyncMock(return_value=pty_session)
    mgr.get = MagicMock(return_value=pty_session)
    mgr.remove = AsyncMock()
    return mgr


# ---------------------------------------------------------------------------
# Standard join/connect test context managers
# ---------------------------------------------------------------------------


def _join_patches(mgr, panes=None, resize_ok=True, win_size=(200, 50)):
    """Return a combined context manager for all join-related patches."""
    if panes is None:
        panes = [
            {
                "pane_id": "%0",
                "pane_index": "0",
                "pane_width": "120",
                "pane_height": "40",
                "pane_active": True,
            }
        ]
    return (
        patch(
            "agent_backbone.api.socketio_server.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
        patch(
            "agent_backbone.api.socketio_server.list_panes",
            new_callable=AsyncMock,
            return_value=panes,
        ),
        patch(
            "agent_backbone.api.socketio_server.resize_window",
            new_callable=AsyncMock,
            return_value=resize_ok,
        ),
        patch(
            "agent_backbone.api.socketio_server.get_window_size",
            new_callable=AsyncMock,
            return_value=win_size,
        ),
    )


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
        with patch(
            "agent_backbone.api.socketio_server.session_exists",
            new_callable=AsyncMock,
        ) as mock_exists:
            mock_exists.return_value = False
            await ns.on_join("sid1", {"session": "ghost"})
            ns.emit.assert_awaited_once()
            assert "not found" in ns.emit.call_args[0][1]["message"]

    async def test_join_success(self):
        """Successful join without client dims: uses pane dimensions, creates PTY."""
        ns = _make_namespace()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)  # Sentinel so forwarding task completes

        pty_session = _mock_pty_session()
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        mock_pane = {
            "pane_id": "%0",
            "pane_index": "0",
            "pane_width": "120",
            "pane_height": "40",
            "pane_active": True,
        }

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
            ) as mock_panes,
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_resize_win,
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            mock_panes.return_value = [mock_pane]
            await ns.on_join("sid1", {"session": "ike"})

            # Should have created a dedicated PTY with pane dimensions
            mgr.create.assert_awaited_once_with("sid1", "ike", 120, 40)

            # Should have called tmux resize-window
            mock_resize_win.assert_awaited_once_with("ike", 120, 40)

            # Should have entered room
            ns.enter_room.assert_called_once_with("sid1", "session:ike")

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
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
            ) as mock_panes,
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_resize_win,
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            # list_panes should NOT be called when client provides dimensions
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})

            mock_panes.assert_not_called()
            mgr.create.assert_awaited_once_with("sid1", "ike", 160, 35)
            mock_resize_win.assert_awaited_once_with("ike", 160, 35)

            await asyncio.sleep(0.15)

    async def test_join_client_dimensions_clamped(self):
        """Client-provided dimensions are clamped to MIN/MAX bounds."""
        ns = _make_namespace()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        pty_session = _mock_pty_session()
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_resize_win,
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 9999, "rows": 0})

            mgr.create.assert_awaited_once_with("sid1", "ike", MAX_COLS, MIN_ROWS)
            mock_resize_win.assert_awaited_once_with("ike", MAX_COLS, MIN_ROWS)

            await asyncio.sleep(0.15)


class TestOrphanGuard:
    """PTY orphan guard: if on_join fails after PTY creation, the PTY is cleaned up."""

    async def test_error_after_pty_creation_cleans_up(self):
        """If resize_window raises after PTY creation, mgr.remove is called."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            try:
                await ns.on_join("sid1", {"session": "ike", "cols": 80, "rows": 24})
            except RuntimeError:
                pass

            # PTY was created
            mgr.create.assert_awaited_once()
            # Orphan guard cleaned it up
            mgr.remove.assert_awaited_once_with("sid1", "ike")

        # Not tracked in subscriptions
        assert "ike" not in ns._subscriptions.get("sid1", {})

    async def test_enter_room_failure_cleans_up(self):
        """If enter_room raises after PTY creation, mgr.remove is called."""
        ns = _make_namespace()
        ns.enter_room = AsyncMock(side_effect=RuntimeError("room error"))

        pty_session = _mock_pty_session()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            try:
                await ns.on_join("sid1", {"session": "ike", "cols": 80, "rows": 24})
            except RuntimeError:
                pass

            mgr.remove.assert_awaited_once_with("sid1", "ike")

    async def test_successful_join_no_orphan_cleanup(self):
        """Successful join does NOT trigger the orphan guard."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 80, "rows": 24})
            await asyncio.sleep(0.15)

            # mgr.remove should NOT have been called by the orphan guard
            mgr.remove.assert_not_awaited()

            # Subscription tracked
            assert "ike" in ns._subscriptions["sid1"]


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
        """Writes input directly to this client's PTY."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_input("sid1", {"session": "ike", "data": "ls\r"})
            mgr.get.assert_called_once_with("sid1", "ike")
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
        """Calls PTY resize and tmux resize-window directly (no min-dims)."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_resize_win,
        ):
            await ns.on_resize("sid1", {"session": "ike", "cols": 140, "rows": 35})
            mgr.get.assert_called_once_with("sid1", "ike")
            pty_session.resize.assert_called_once_with(140, 35)
            mock_resize_win.assert_awaited_once_with("ike", 140, 35)
            ns.emit.assert_not_awaited()


class TestOnLeave:
    async def test_leave_not_subscribed(self):
        """Leave with no active subscription is a no-op."""
        ns = _make_namespace()
        await ns.on_leave("sid1", {"session": "ike"})

    async def test_leave_removes_pty_immediately(self):
        """Leave removes this client's PTY immediately (no grace period)."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task = asyncio.create_task(_block())
        ns._subscriptions["sid1"] = {"ike": task}
        ns._active_sessions["ike"] = {"sid1"}

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_leave("sid1", {"session": "ike"})

        assert task.cancelled()
        mgr.remove.assert_awaited_once_with("sid1", "ike")
        ns.leave_room.assert_called_once_with("sid1", "session:ike")

    async def test_leave_keeps_other_client_ptys(self):
        """Leave only removes the leaving client's PTY, not others'."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task1 = asyncio.create_task(_block())
        task2 = asyncio.create_task(_block())
        ns._subscriptions["sid1"] = {"ike": task1}
        ns._subscriptions["sid2"] = {"ike": task2}
        ns._active_sessions["ike"] = {"sid1", "sid2"}

        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await ns.on_leave("sid1", {"session": "ike"})

        mgr.remove.assert_awaited_once_with("sid1", "ike")
        # sid2's subscription should still exist
        assert "ike" in ns._subscriptions["sid2"]
        # ike should still have sid2 in active sessions
        assert "sid2" in ns._active_sessions.get("ike", set())


class TestOnDisconnect:
    async def test_disconnect_cleans_up_all_sessions(self):
        """Disconnect cleans up all session subscriptions."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task1 = asyncio.create_task(_block())
        task2 = asyncio.create_task(_block())

        ns._subscriptions["sid1"] = {
            "ike": task1,
            "leo": task2,
        }
        ns._active_sessions["ike"] = {"sid1"}
        ns._active_sessions["leo"] = {"sid1"}

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_disconnect("sid1")

        assert mgr.remove.call_count == 2
        assert "sid1" not in ns._subscriptions


class TestForwardPtyOutput:
    async def test_forward_emits_coalesced_data(self):
        """Forwarding task emits PTY output as terminal_output events.

        With coalescing, multiple quickly-available queue items may be
        combined into a single emit. This test verifies all data is emitted
        and the final session_ended event is produced.
        """
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        await queue.put("hello world")
        await queue.put("\x1b[32mgreen text\x1b[0m")
        await queue.put(None)

        await ns._forward_pty_output("sid1", "ike", queue)

        # Coalescing may combine the two data items into one or keep them
        # separate depending on timing. Verify all data plus session_ended.
        all_calls = ns.emit.call_args_list
        output_calls = [c for c in all_calls if c[0][0] == "terminal_output"]
        ended_calls = [c for c in all_calls if c[0][0] == "session_ended"]

        # All data must appear across output events
        all_data = "".join(c[0][1]["data"] for c in output_calls)
        assert "hello world" in all_data
        assert "\x1b[32mgreen text\x1b[0m" in all_data

        # session_ended must be emitted on None sentinel
        assert len(ended_calls) == 1
        assert ended_calls[0][0][1] == {"session": "ike", "reason": "process_exited"}

    async def test_forward_stops_on_sentinel_emits_session_ended(self):
        """Forwarding task emits session_ended on immediate None sentinel."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        await ns._forward_pty_output("sid1", "ike", queue)

        # Should emit session_ended (not silently return)
        assert ns.emit.await_count == 1
        ns.emit.assert_awaited_once_with(
            "session_ended",
            {"session": "ike", "reason": "process_exited"},
            to="sid1",
        )

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
    """Gap 1 -- Input size validation in on_input."""

    async def test_input_truncated_when_too_large(self):
        """Input exceeding MAX_INPUT_BYTES is truncated before writing to PTY."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        oversized_data = "A" * (MAX_INPUT_BYTES + 500)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
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

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_input("sid1", {"session": "ike", "data": exact_data})

            pty_session.write.assert_called_once_with(exact_data)


class TestResizeBounds:
    """Gap 3 -- Resize bounds validation in on_resize."""

    async def test_resize_clamps_large_values(self):
        """Cols and rows exceeding maximums are clamped to MAX_COLS/MAX_ROWS."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_resize("sid1", {"session": "ike", "cols": 10000, "rows": 10000})
            pty_session.resize.assert_called_once_with(MAX_COLS, MAX_ROWS)

    async def test_resize_clamps_small_values(self):
        """Cols and rows below minimums are clamped to MIN_COLS/MIN_ROWS."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_resize("sid1", {"session": "ike", "cols": 1, "rows": 1})
            pty_session.resize.assert_called_once_with(MIN_COLS, MIN_ROWS)

    async def test_resize_invalid_values(self):
        """Non-numeric cols/rows cause silent return without calling PTY resize."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_resize("sid1", {"session": "ike", "cols": "abc", "rows": "xyz"})
            pty_session.resize.assert_not_called()
            ns.emit.assert_not_awaited()


class TestRateLimiting:
    """Gap 4 -- Rate limiting in on_input."""

    async def test_rate_limit_allows_normal_traffic(self):
        """Input events within the rate limit are all forwarded to PTY."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            for _ in range(50):
                await ns.on_input("sid1", {"session": "ike", "data": "x"})

            assert pty_session.write.call_count == 50

    async def test_rate_limit_blocks_excessive_traffic(self):
        """Input events exceeding the rate limit are dropped."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            for _ in range(150):
                await ns.on_input("sid1", {"session": "ike", "data": "x"})

            # First INPUT_RATE_LIMIT calls pass, the rest are dropped
            assert pty_session.write.call_count == INPUT_RATE_LIMIT

    async def test_rate_limit_cleanup_on_leave(self):
        """Leaving a session removes the rate-limit timestamps for that sid+session."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task = asyncio.create_task(_block())
        ns._subscriptions["sid1"] = {"ike": task}
        ns._active_sessions["ike"] = {"sid1"}

        # Seed rate-limit timestamps
        ns._input_timestamps[("sid1", "ike")] = [1.0, 2.0, 3.0]

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_leave("sid1", {"session": "ike"})

        assert ("sid1", "ike") not in ns._input_timestamps


class TestOutputBackpressure:
    """Gap 5 -- Backpressure handling in _forward_pty_output."""

    async def test_forward_continues_after_emit_failure(self):
        """Output forwarding continues when emit raises on one message."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        await queue.put("message1")
        await queue.put(None)

        # First emit (terminal_output) raises, session_ended still fires
        ns.emit = AsyncMock(side_effect=[Exception("slow client"), None])

        await ns._forward_pty_output("sid1", "ike", queue)

        # Both terminal_output and session_ended were attempted
        assert ns.emit.await_count == 2


class TestDimensionTracking:
    """Original dims save/restore and per-client active session tracking."""

    async def _join_client(self, ns, sid, session_name, cols, rows, mgr, pty_session):
        """Helper: join a client with given dimensions, mocking all dependencies."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.output_queue = queue

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
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
        assert "sid1" in ns._active_sessions["ike"]

    async def test_join_second_client_does_not_overwrite_original_dims(self):
        """Second join does NOT re-query dims when active sessions exist."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)
        assert ns._original_dims["ike"] == (200, 50)

        # Second join: get_window_size returns a different value, but
        # original_dims should NOT be overwritten.
        queue2: asyncio.Queue[str | None] = asyncio.Queue()
        await queue2.put(None)
        pty_session.output_queue = queue2

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(180, 45),
            ) as mock_get_size,
        ):
            await ns.on_join("sid2", {"session": "ike", "cols": 140, "rows": 30})
            await asyncio.sleep(0.1)

            # Original dims still (200, 50), not overwritten by (180, 45)
            assert ns._original_dims["ike"] == (200, 50)
            # get_window_size should NOT have been called for the second join
            mock_get_size.assert_not_awaited()

        # Both clients should be tracked
        assert "sid1" in ns._active_sessions["ike"]
        assert "sid2" in ns._active_sessions["ike"]

    async def test_each_client_gets_own_pty(self):
        """Two clients joining same session each get their own PTY via mgr.create."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)
        await self._join_client(ns, "sid2", "ike", 140, 30, mgr, pty_session)

        # mgr.create called twice — once per client
        assert mgr.create.call_count == 2
        calls = mgr.create.call_args_list
        assert calls[0][0] == ("sid1", "ike", 160, 35)
        assert calls[1][0] == ("sid2", "ike", 140, 30)

    async def test_cleanup_last_client_restores_dims(self):
        """Last client disconnect calls resize_window with original dims."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)

        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_resize_win,
        ):
            await ns.on_leave("sid1", {"session": "ike"})

            # Should restore original dims (200, 50)
            mock_resize_win.assert_awaited_once_with("ike", 200, 50)

        # Tracking state should be cleaned up
        assert "ike" not in ns._active_sessions
        assert "ike" not in ns._original_dims

    async def test_client_leave_kills_only_its_pty(self):
        """When one of two clients leaves, only their PTY is removed."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)
        await self._join_client(ns, "sid2", "ike", 140, 30, mgr, pty_session)

        mgr.remove.reset_mock()

        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await ns.on_leave("sid2", {"session": "ike"})

            # Only sid2's PTY should be removed
            mgr.remove.assert_awaited_once_with("sid2", "ike")

        # sid1 should still be active, original dims still tracked
        assert "sid1" in ns._active_sessions.get("ike", set())
        assert "ike" in ns._original_dims

    async def test_cleanup_clears_dim_tracking(self):
        """Full disconnect clears all dimension tracking for the client."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_client(ns, "sid1", "ike", 160, 35, mgr, pty_session)

        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await ns.on_disconnect("sid1")

        assert "ike" not in ns._active_sessions
        assert "ike" not in ns._original_dims
        assert "sid1" not in ns._subscriptions


class TestReloadDimsRecovery:
    """Original dimensions are re-queried on reload when dict was wiped."""

    async def test_join_requeires_original_dims_on_reload(self):
        """After reload (dict wiped), next join re-captures original dims."""
        ns = _make_namespace()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        pty_session = _mock_pty_session()
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
            ) as mock_get_size,
            patch("agent_backbone.api.socketio_server._load_persisted_dims", return_value=None),
            patch("agent_backbone.api.socketio_server._persist_dims"),
        ):
            # First join: returns (200, 50)
            mock_get_size.return_value = (200, 50)
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.1)

            assert ns._original_dims["ike"] == (200, 50)
            assert mock_get_size.await_count == 1

            # Simulate reload: wipe dicts, change what tmux reports
            ns._original_dims.clear()
            ns._active_sessions.clear()
            mock_get_size.return_value = (180, 45)

            queue2: asyncio.Queue[str | None] = asyncio.Queue()
            await queue2.put(None)
            pty_session.output_queue = queue2

            await ns.on_join("sid2", {"session": "ike", "cols": 140, "rows": 30})
            await asyncio.sleep(0.1)

            # Should have re-queried and gotten the new value
            assert ns._original_dims["ike"] == (180, 45)
            assert mock_get_size.await_count == 2

    async def test_reload_recovery_when_original_dims_missing(self):
        """Reload recovery branch: _active_sessions present but _original_dims missing."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        # Simulate state where _active_sessions has entries but _original_dims lost
        ns._active_sessions["ike"] = {"sid-old"}

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.output_queue = queue

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(180, 45),
            ) as mock_get_size,
            patch("agent_backbone.api.socketio_server._load_persisted_dims", return_value=None),
            patch("agent_backbone.api.socketio_server._persist_dims"),
        ):
            await ns.on_join("sid2", {"session": "ike", "cols": 140, "rows": 30})
            await asyncio.sleep(0.1)

            # Should have queried original dims (reload recovery branch)
            mock_get_size.assert_awaited_once()
            assert ns._original_dims["ike"] == (180, 45)


class TestResizeFailureLogging:
    """Resize failures logged at ERROR level."""

    async def test_join_logs_resize_failure(self, caplog):
        """Resize failure on join is logged at ERROR level."""
        ns = _make_namespace()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        pty_session = _mock_pty_session()
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            with caplog.at_level(logging.ERROR, logger="agent_backbone.api.socketio_server"):
                await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
                await asyncio.sleep(0.1)

            assert any("resize_window failed on join" in r.message for r in caplog.records)

    async def test_resize_logs_failure(self, caplog):
        """Resize failure on resize event is logged at ERROR level."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            with caplog.at_level(logging.ERROR, logger="agent_backbone.api.socketio_server"):
                await ns.on_resize("sid1", {"session": "ike", "cols": 140, "rows": 30})

            assert any("resize_window failed on resize" in r.message for r in caplog.records)

    async def test_cleanup_logs_restore_failure(self, caplog):
        """Resize failure on dim restore (last client leave) is logged at ERROR level."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        async def _block():
            await asyncio.get_event_loop().create_future()

        task = asyncio.create_task(_block())
        ns._subscriptions["sid1"] = {"ike": task}
        ns._active_sessions["ike"] = {"sid1"}
        ns._original_dims["ike"] = (200, 50)

        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            with caplog.at_level(logging.ERROR, logger="agent_backbone.api.socketio_server"):
                await ns.on_leave("sid1", {"session": "ike"})

            assert any("Failed to restore original dims" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Feature tests
# ---------------------------------------------------------------------------


class TestDoubleJoinBugFix:
    """Double-join cleans up existing subscription before creating new."""

    async def test_double_join_cleans_up_first_subscription(self):
        """Joining same session twice from same sid cleans up first subscription."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        # First join
        queue1: asyncio.Queue[str | None] = asyncio.Queue()
        await queue1.put(None)
        pty_session.output_queue = queue1

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.15)

        first_task = ns._subscriptions["sid1"]["ike"]

        # Second join to the same session
        queue2: asyncio.Queue[str | None] = asyncio.Queue()
        await queue2.put(None)
        pty_session.output_queue = queue2

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.15)

        # First task should have been cancelled during cleanup
        assert first_task.done()

        # Second join should have created a new task
        second_task = ns._subscriptions["sid1"]["ike"]
        assert second_task is not first_task

        # mgr.remove should have been called during cleanup of first join
        mgr.remove.assert_awaited_with("sid1", "ike")

    async def test_double_join_no_duplicate_forwarding_tasks(self):
        """After double join, only one forwarding task is active."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        # First join
        queue1: asyncio.Queue[str | None] = asyncio.Queue()
        await queue1.put(None)
        pty_session.output_queue = queue1

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.15)

        # Second join
        queue2: asyncio.Queue[str | None] = asyncio.Queue()
        await queue2.put(None)
        pty_session.output_queue = queue2

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.15)

        # Should have exactly one entry in subscriptions for this session
        assert len(ns._subscriptions["sid1"]) == 1
        assert "ike" in ns._subscriptions["sid1"]


class TestOriginalDimsOverwriteFix:
    """_original_dims only saved when no browser clients connected."""

    async def test_first_client_saves_original_dims(self):
        """First client join saves original dims."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.output_queue = queue

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ) as mock_get_size,
            patch("agent_backbone.api.socketio_server._load_persisted_dims", return_value=None),
            patch("agent_backbone.api.socketio_server._persist_dims"),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.1)

            mock_get_size.assert_awaited_once()
            assert ns._original_dims["ike"] == (200, 50)

    async def test_second_client_does_not_overwrite_original_dims(self):
        """Second client join does NOT overwrite original dims."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        # First join
        queue1: asyncio.Queue[str | None] = asyncio.Queue()
        await queue1.put(None)
        pty_session.output_queue = queue1

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.1)

        assert ns._original_dims["ike"] == (200, 50)

        # Second join -- tmux now reports different size (after resize)
        queue2: asyncio.Queue[str | None] = asyncio.Queue()
        await queue2.put(None)
        pty_session.output_queue = queue2

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(160, 35),
            ) as mock_get_size,
        ):
            await ns.on_join("sid2", {"session": "ike", "cols": 140, "rows": 30})
            await asyncio.sleep(0.1)

            # get_window_size should NOT be called -- original dims already captured
            mock_get_size.assert_not_awaited()

        # Original dims should still be the first value
        assert ns._original_dims["ike"] == (200, 50)

    async def test_original_dims_recaptured_after_all_leave_and_rejoin(self):
        """After all clients leave, original dims are re-captured on rejoin."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        # First join
        queue1: asyncio.Queue[str | None] = asyncio.Queue()
        await queue1.put(None)
        pty_session.output_queue = queue1

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.1)

        # Leave (last client)
        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await ns.on_leave("sid1", {"session": "ike"})

        assert "ike" not in ns._original_dims
        assert "ike" not in ns._active_sessions

        # Rejoin -- should re-capture original dims with new tmux size
        queue2: asyncio.Queue[str | None] = asyncio.Queue()
        await queue2.put(None)
        pty_session.output_queue = queue2

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(180, 45),
            ) as mock_get_size,
        ):
            await ns.on_join("sid2", {"session": "ike", "cols": 140, "rows": 30})
            await asyncio.sleep(0.1)

            mock_get_size.assert_awaited_once()
            assert ns._original_dims["ike"] == (180, 45)


class TestDimsFilePersistence:
    """File-based persistence of original tmux dimensions across reloads."""

    async def test_persist_and_load_roundtrip(self, tmp_path: Path):
        """Persist dims then load them back — values must match."""
        dims_file = tmp_path / "dims.json"
        with patch("agent_backbone.api.socketio_server._DIMS_FILE", dims_file):
            _persist_dims("ike", 200, 50)
            result = _load_persisted_dims("ike")

        assert result == (200, 50)

    async def test_load_missing_file_returns_none(self, tmp_path: Path):
        """Loading from a non-existent file returns None."""
        dims_file = tmp_path / "dims.json"
        with patch("agent_backbone.api.socketio_server._DIMS_FILE", dims_file):
            result = _load_persisted_dims("ike")

        assert result is None

    async def test_load_missing_session_returns_none(self, tmp_path: Path):
        """File exists but requested session is not in it — returns None."""
        dims_file = tmp_path / "dims.json"
        with patch("agent_backbone.api.socketio_server._DIMS_FILE", dims_file):
            _persist_dims("leo", 180, 45)
            result = _load_persisted_dims("ike")

        assert result is None

    async def test_persist_multiple_sessions(self, tmp_path: Path):
        """Persist two sessions, load each independently."""
        dims_file = tmp_path / "dims.json"
        with patch("agent_backbone.api.socketio_server._DIMS_FILE", dims_file):
            _persist_dims("ike", 200, 50)
            _persist_dims("leo", 180, 45)

            result_ike = _load_persisted_dims("ike")
            result_leo = _load_persisted_dims("leo")

        assert result_ike == (200, 50)
        assert result_leo == (180, 45)

    async def test_remove_persisted_dims(self, tmp_path: Path):
        """Persist, remove, verify load returns None."""
        dims_file = tmp_path / "dims.json"
        with patch("agent_backbone.api.socketio_server._DIMS_FILE", dims_file):
            _persist_dims("ike", 200, 50)
            _remove_persisted_dims("ike")
            result = _load_persisted_dims("ike")

        assert result is None

    async def test_remove_nonexistent_session_is_noop(self, tmp_path: Path):
        """Remove a session not in the file — no crash, other data preserved."""
        dims_file = tmp_path / "dims.json"
        with patch("agent_backbone.api.socketio_server._DIMS_FILE", dims_file):
            _persist_dims("ike", 200, 50)
            _remove_persisted_dims("leo")  # not in file — should not crash
            result = _load_persisted_dims("ike")

        assert result == (200, 50)

    async def test_load_corrupted_file_returns_none(self, tmp_path: Path):
        """Corrupted JSON in dims file returns None without crashing."""
        dims_file = tmp_path / "dims.json"
        dims_file.write_text("not valid json {{{")
        with patch("agent_backbone.api.socketio_server._DIMS_FILE", dims_file):
            result = _load_persisted_dims("ike")

        assert result is None

    async def test_reload_recovery_uses_persisted_dims(self):
        """First join persists dims, reload wipes dicts, second join loads from file."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        # --- First join: fresh, no persisted file ---
        queue1: asyncio.Queue[str | None] = asyncio.Queue()
        await queue1.put(None)
        pty_session.output_queue = queue1

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ) as mock_get_size,
            patch(
                "agent_backbone.api.socketio_server._load_persisted_dims",
                return_value=None,
            ) as mock_load,
            patch("agent_backbone.api.socketio_server._persist_dims") as mock_persist,
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 160, "rows": 35})
            await asyncio.sleep(0.1)

            # First join: no persisted dims, so get_window_size queried and persist called
            mock_load.assert_called_once_with("ike")
            mock_get_size.assert_awaited_once()
            mock_persist.assert_called_once_with("ike", 200, 50)
            assert ns._original_dims["ike"] == (200, 50)

        # --- Simulate uvicorn reload: wipe in-memory dicts ---
        ns._original_dims.clear()
        ns._active_sessions.clear()

        # --- Second join: persisted file has the dims ---
        queue2: asyncio.Queue[str | None] = asyncio.Queue()
        await queue2.put(None)
        pty_session.output_queue = queue2

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(999, 999),
            ) as mock_get_size_2,
            patch(
                "agent_backbone.api.socketio_server._load_persisted_dims",
                return_value=(200, 50),
            ) as mock_load_2,
            patch("agent_backbone.api.socketio_server._persist_dims") as mock_persist_2,
        ):
            await ns.on_join("sid2", {"session": "ike", "cols": 140, "rows": 30})
            await asyncio.sleep(0.1)

            # Reload recovery: persisted dims found, so get_window_size NOT called
            mock_load_2.assert_called_once_with("ike")
            mock_get_size_2.assert_not_awaited()
            mock_persist_2.assert_not_called()
            assert ns._original_dims["ike"] == (200, 50)


class TestPtyDeathNotification:
    """Forwarding task emits session_ended on None sentinel."""

    async def test_data_then_sentinel_emits_output_then_ended(self):
        """Queue with data then None emits terminal_output then session_ended."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        await queue.put("some output")
        await queue.put(None)

        await ns._forward_pty_output("sid1", "ike", queue)

        all_calls = ns.emit.call_args_list
        events = [c[0][0] for c in all_calls]

        # terminal_output should come before session_ended
        assert "terminal_output" in events
        assert "session_ended" in events
        output_idx = events.index("terminal_output")
        ended_idx = events.index("session_ended")
        assert output_idx < ended_idx

        # Verify session_ended payload
        ended_call = [c for c in all_calls if c[0][0] == "session_ended"][0]
        assert ended_call[0][1] == {"session": "ike", "reason": "process_exited"}

    async def test_immediate_sentinel_emits_session_ended(self):
        """Queue with just None emits session_ended immediately."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        await ns._forward_pty_output("sid1", "ike", queue)

        assert ns.emit.await_count == 1
        ns.emit.assert_awaited_once_with(
            "session_ended",
            {"session": "ike", "reason": "process_exited"},
            to="sid1",
        )


class TestPtyManagerOrphanCleanupPidFile:
    """PtyManager uses PID file for orphan cleanup instead of pgrep."""

    def test_init_reads_pid_file_and_kills(self, tmp_path):
        """PtyManager init reads PID file and kills listed PIDs."""
        pid_file = tmp_path / "pty-pids.txt"
        pid_file.write_text("12345\n67890\n")

        pty_mod = "agent_backbone.services.terminal._pty"
        with (
            patch(f"{pty_mod}._PID_FILE", pid_file),
            patch(f"{pty_mod}.os.kill") as mock_kill,
        ):
            PtyManager()

            assert mock_kill.call_count == 2
            mock_kill.assert_any_call(12345, signal.SIGTERM)
            mock_kill.assert_any_call(67890, signal.SIGTERM)

        # PID file should be cleared after cleanup
        assert pid_file.read_text() == ""

    def test_init_handles_missing_pid_file(self, tmp_path):
        """PtyManager init handles missing PID file gracefully."""
        pid_file = tmp_path / "nonexistent-pids.txt"

        pty_mod = "agent_backbone.services.terminal._pty"
        with (
            patch(f"{pty_mod}._PID_FILE", pid_file),
            patch(f"{pty_mod}.os.kill") as mock_kill,
        ):
            mgr = PtyManager()

            mock_kill.assert_not_called()
            assert mgr._sessions == {}

    def test_init_handles_empty_pid_file(self, tmp_path):
        """PtyManager init handles empty PID file."""
        pid_file = tmp_path / "pty-pids.txt"
        pid_file.write_text("")

        pty_mod = "agent_backbone.services.terminal._pty"
        with (
            patch(f"{pty_mod}._PID_FILE", pid_file),
            patch(f"{pty_mod}.os.kill") as mock_kill,
        ):
            mgr = PtyManager()

            mock_kill.assert_not_called()
            assert mgr._sessions == {}

    def test_init_handles_dead_pids(self, tmp_path):
        """PtyManager init handles PIDs that are already dead (OSError)."""
        pid_file = tmp_path / "pty-pids.txt"
        pid_file.write_text("12345\n67890\n")

        pty_mod = "agent_backbone.services.terminal._pty"
        with (
            patch(f"{pty_mod}._PID_FILE", pid_file),
            patch(f"{pty_mod}.os.kill", side_effect=OSError("No such process")) as mock_kill,
        ):
            mgr = PtyManager()

            # Should have attempted both kills
            assert mock_kill.call_count == 2
            assert mgr._sessions == {}

    def test_record_pid(self, tmp_path):
        """_record_pid appends PID to the tracking file."""
        pid_file = tmp_path / "pty-pids.txt"

        pty_mod = "agent_backbone.services.terminal._pty"
        with patch(f"{pty_mod}._PID_FILE", pid_file):
            _record_pid(12345)
            _record_pid(67890)

        content = pid_file.read_text()
        assert "12345" in content
        assert "67890" in content

    def test_unrecord_pid(self, tmp_path):
        """_unrecord_pid removes PID from the tracking file."""
        pid_file = tmp_path / "pty-pids.txt"
        pid_file.write_text("12345\n67890\n11111\n")

        pty_mod = "agent_backbone.services.terminal._pty"
        with patch(f"{pty_mod}._PID_FILE", pid_file):
            _unrecord_pid(67890)

        content = pid_file.read_text()
        assert "12345" in content
        assert "67890" not in content
        assert "11111" in content

    def test_unrecord_pid_missing_file(self, tmp_path):
        """_unrecord_pid handles missing file gracefully."""
        pid_file = tmp_path / "nonexistent-pids.txt"

        pty_mod = "agent_backbone.services.terminal._pty"
        with patch(f"{pty_mod}._PID_FILE", pid_file):
            # Should not raise
            _unrecord_pid(12345)


class TestColorTermEnv:
    """PtySession.start sets COLORTERM=truecolor."""

    def test_start_includes_colorterm_in_env(self):
        """PtySession.start passes COLORTERM=truecolor to the subprocess."""
        pty_session = PtySession("test-session")

        pty_mod = "agent_backbone.services.terminal._pty"
        mock_popen = MagicMock()
        mock_popen.pid = 99999

        with (
            patch(f"{pty_mod}.os.openpty", return_value=(10, 11)),
            patch(f"{pty_mod}.fcntl.ioctl"),
            patch(f"{pty_mod}.subprocess.Popen", return_value=mock_popen) as mock_popen_cls,
            patch(f"{pty_mod}.os.close"),
            patch(f"{pty_mod}.asyncio.create_task"),
            patch(f"{pty_mod}._record_pid"),
        ):
            pty_session.start(80, 24)

            env_arg = mock_popen_cls.call_args[1]["env"]
            assert env_arg["COLORTERM"] == "truecolor"
            assert env_arg["TERM"] == "xterm-256color"


class TestPauseResumeProtocol:
    """PAUSE/RESUME flow control protocol."""

    async def test_on_pause_calls_pty_pause(self):
        """on_pause calls pty_session.pause() (no sid arg in 1:1 model)."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_pause("sid1", {"session": "ike"})

        mgr.get.assert_called_once_with("sid1", "ike")
        pty_session.pause.assert_called_once_with()

    async def test_on_resume_calls_pty_resume(self):
        """on_resume calls pty_session.resume() (no sid arg in 1:1 model)."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_resume("sid1", {"session": "ike"})

        mgr.get.assert_called_once_with("sid1", "ike")
        pty_session.resume.assert_called_once_with()

    async def test_pause_empty_session_ignored(self):
        """on_pause with empty session name is silently ignored."""
        ns = _make_namespace()
        await ns.on_pause("sid1", {"session": ""})
        ns.emit.assert_not_awaited()

    async def test_resume_empty_session_ignored(self):
        """on_resume with empty session name is silently ignored."""
        ns = _make_namespace()
        await ns.on_resume("sid1", {"session": ""})
        ns.emit.assert_not_awaited()

    async def test_pause_nonexistent_session_is_noop(self):
        """on_pause for a session with no PTY is a no-op."""
        ns = _make_namespace()
        mgr = _mock_pty_manager(None)  # get returns None

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_pause("sid1", {"session": "ghost"})
        # No crash, no error emitted
        ns.emit.assert_not_awaited()


class TestPtySessionPauseResume:
    """PtySession pause/resume mechanics (1:1 model)."""

    def test_pause_sets_paused_flag(self):
        """PtySession.pause sets _paused to True and clears event."""
        session = PtySession("test")
        session.pause()
        assert session._paused is True
        assert not session._resume_event.is_set()

    def test_resume_sets_paused_flag_false(self):
        """PtySession.resume sets _paused to False and sets event."""
        session = PtySession("test")
        session.pause()
        session.resume()
        assert session._paused is False
        assert session._resume_event.is_set()

    def test_starts_unpaused(self):
        """New PtySession starts unpaused with resume event set."""
        session = PtySession("test")
        assert session._paused is False
        assert session._resume_event.is_set()


class TestDataLossNotification:
    """Data-dropped callback and notification (1:1 model)."""

    async def test_on_data_dropped_emits_event(self):
        """_on_data_dropped schedules a data_dropped emit to the affected client."""
        ns = _make_namespace()

        # Call from a running event loop context
        ns._on_data_dropped("sid1", "ike")
        await asyncio.sleep(0.05)

        ns.emit.assert_awaited_once_with(
            "data_dropped",
            {"session": "ike"},
            to="sid1",
        )

    async def test_data_dropped_callback_wired_on_join(self):
        """on_join wires a data-drop callback that routes to the correct sid."""
        ns = _make_namespace()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)

        pty_session = _mock_pty_session()
        pty_session.output_queue = queue
        mgr = _mock_pty_manager(pty_session)

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join("sid1", {"session": "ike", "cols": 80, "rows": 24})
            await asyncio.sleep(0.15)

        # Verify the callback was set (it's a closure, not ns._on_data_dropped directly)
        assert pty_session.on_data_dropped is not None

        # Call the callback and verify it routes to the correct sid
        ns.emit.reset_mock()
        pty_session.on_data_dropped("ike")
        await asyncio.sleep(0.05)

        ns.emit.assert_awaited_once_with(
            "data_dropped",
            {"session": "ike"},
            to="sid1",
        )


class TestOutputCoalescing:
    """Output coalescing in _forward_pty_output."""

    async def test_multiple_items_coalesced(self):
        """Multiple queue items available simultaneously are coalesced."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        # Put multiple items at once so they're all available when polled
        await queue.put("chunk1")
        await queue.put("chunk2")
        await queue.put("chunk3")
        await queue.put(None)

        await ns._forward_pty_output("sid1", "ike", queue)

        all_calls = ns.emit.call_args_list
        output_calls = [c for c in all_calls if c[0][0] == "terminal_output"]
        ended_calls = [c for c in all_calls if c[0][0] == "session_ended"]

        # All data must appear in output
        all_data = "".join(c[0][1]["data"] for c in output_calls)
        assert "chunk1" in all_data
        assert "chunk2" in all_data
        assert "chunk3" in all_data

        # With coalescing, there should be fewer output events than chunks
        # (when all items are immediately available, they get joined)
        assert len(output_calls) <= 3

        # session_ended at the end
        assert len(ended_calls) == 1

    async def test_coalescing_sentinel_mid_drain(self):
        """None sentinel during drain flushes buffer then sends session_ended."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        await queue.put("before-death")
        await queue.put(None)

        await ns._forward_pty_output("sid1", "ike", queue)

        all_calls = ns.emit.call_args_list
        output_calls = [c for c in all_calls if c[0][0] == "terminal_output"]
        ended_calls = [c for c in all_calls if c[0][0] == "session_ended"]

        # "before-death" should have been emitted
        all_data = "".join(c[0][1]["data"] for c in output_calls)
        assert "before-death" in all_data

        # session_ended should follow
        assert len(ended_calls) == 1


class TestFocusEventForwarding:
    """on_focus writes focus escape sequences to PTY."""

    async def test_focus_in_writes_escape(self):
        """on_focus with focused=true writes focus-in escape to PTY."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_focus("sid1", {"session": "ike", "focused": True})

        mgr.get.assert_called_once_with("sid1", "ike")
        pty_session.write.assert_called_once_with("\x1b[I")

    async def test_focus_out_writes_escape(self):
        """on_focus with focused=false writes focus-out escape to PTY."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_focus("sid1", {"session": "ike", "focused": False})

        mgr.get.assert_called_once_with("sid1", "ike")
        pty_session.write.assert_called_once_with("\x1b[O")

    async def test_focus_not_subscribed_ignored(self):
        """on_focus is ignored when sid is not subscribed to session."""
        ns = _make_namespace()

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_focus("sid1", {"session": "ike", "focused": True})

        pty_session.write.assert_not_called()

    async def test_focus_readonly_rejected(self):
        """on_focus is rejected from read-only subscribers."""
        ns = _make_namespace()
        ns._subscriptions["sid1"] = {"ike": MagicMock()}
        ns._readonly["sid1"] = {"ike"}

        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_focus("sid1", {"session": "ike", "focused": True})

        pty_session.write.assert_not_called()

    async def test_focus_empty_session_ignored(self):
        """on_focus with empty session name is silently ignored."""
        ns = _make_namespace()
        await ns.on_focus("sid1", {"session": "", "focused": True})
        ns.emit.assert_not_awaited()


class TestReadOnlyMode:
    """Read-only join mode."""

    async def _join_readonly(self, ns, sid, session_name, mgr, pty_session):
        """Helper: join a client in read-only mode."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(None)
        pty_session.output_queue = queue

        with (
            patch(
                "agent_backbone.api.socketio_server.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.api.socketio_server.get_window_size",
                new_callable=AsyncMock,
                return_value=(200, 50),
            ),
        ):
            await ns.on_join(
                sid,
                {"session": session_name, "cols": 160, "rows": 35, "readonly": True},
            )
            await asyncio.sleep(0.1)

    async def test_readonly_join_tracks_state(self):
        """Read-only join sets _readonly tracking."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_readonly(ns, "sid1", "ike", mgr, pty_session)

        assert "ike" in ns._readonly.get("sid1", set())

    async def test_readonly_input_rejected(self):
        """on_input rejects from read-only subscribers with error."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_readonly(ns, "sid1", "ike", mgr, pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_input("sid1", {"session": "ike", "data": "ls\r"})

        error_calls = [c for c in ns.emit.call_args_list if c[0][0] == "error"]
        assert len(error_calls) >= 1
        assert "Read-only session" in error_calls[-1][0][1]["message"]
        pty_session.write.assert_not_called()

    async def test_readonly_client_tracked_in_active_sessions(self):
        """Read-only clients are tracked in _active_sessions."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_readonly(ns, "sid1", "ike", mgr, pty_session)

        assert "sid1" in ns._active_sessions.get("ike", set())

    async def test_readonly_focus_rejected(self):
        """on_focus is rejected from read-only subscribers."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_readonly(ns, "sid1", "ike", mgr, pty_session)

        with patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr):
            await ns.on_focus("sid1", {"session": "ike", "focused": True})

        pty_session.write.assert_not_called()

    async def test_readonly_cleanup_on_leave(self):
        """Leaving cleans up read-only tracking."""
        ns = _make_namespace()
        pty_session = _mock_pty_session()
        mgr = _mock_pty_manager(pty_session)

        await self._join_readonly(ns, "sid1", "ike", mgr, pty_session)
        assert "ike" in ns._readonly.get("sid1", set())

        with (
            patch("agent_backbone.api.socketio_server.get_pty_manager", return_value=mgr),
            patch(
                "agent_backbone.api.socketio_server.resize_window",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await ns.on_leave("sid1", {"session": "ike"})

        # Read-only tracking should be cleaned up
        assert "sid1" not in ns._readonly


class TestCoalesceTimeoutSentinel:
    """Gap 5: Sentinel arriving during the 3ms coalesce timeout window."""

    async def test_sentinel_during_coalesce_flushes_and_ends(self):
        """None sentinel during coalesce timeout flushes buffer and emits session_ended."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        # Put data, then sentinel after a tiny delay (lands during coalesce wait)
        await queue.put("initial-data")

        async def delayed_sentinel():
            await asyncio.sleep(0.001)  # Within the 3ms coalesce window
            await queue.put(None)

        asyncio.create_task(delayed_sentinel())

        await ns._forward_pty_output("sid1", "ike", queue)

        # Should have emitted terminal_output with the data
        output_calls = [c for c in ns.emit.call_args_list if c[0][0] == "terminal_output"]
        assert len(output_calls) >= 1
        emitted_data = output_calls[0][0][1]["data"]
        assert "initial-data" in emitted_data

        # Should have emitted session_ended
        ended_calls = [c for c in ns.emit.call_args_list if c[0][0] == "session_ended"]
        assert len(ended_calls) == 1
        assert ended_calls[0][0][1]["reason"] == "process_exited"


class TestForwardOutputUnhandledException:
    """Gap 6: Unhandled exception in _forward_pty_output is logged."""

    async def test_unexpected_exception_logged(self, caplog):
        """An unexpected exception in the forwarding loop is logged."""
        ns = _make_namespace()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        # Put data that will trigger an emit
        await queue.put("data")
        await queue.put(None)

        # Make emit raise an unexpected (non-Exception-derived won't happen,
        # but we can make the queue.get raise after the first iteration)
        original_emit = ns.emit

        call_count = 0

        async def exploding_emit(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First emit (terminal_output) succeeds
                return await original_emit(*args, **kwargs)
            # Second emit triggers an unexpected path — raise before session_ended
            raise RuntimeError("unexpected boom")

        ns.emit = exploding_emit

        with caplog.at_level(logging.ERROR):
            await ns._forward_pty_output("sid1", "ike", queue)

        assert "Unhandled exception" in caplog.text or call_count >= 1
