"""Tests for src/stream_broker.py."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import BackboneConfig, ControlModeConfig
from src.control_mode import (
    ExitEvent,
    LayoutChangeEvent,
    ModeChangeEvent,
    OutputEvent,
    SessionEvent,
    WindowEvent,
)
from src.stream_broker import (
    StreamBroker,
    _event_to_data,
    _event_type_name,
    format_sse,
    parse_sse_frame,
)


class TestEventFormatting:
    def test_event_type_name_output(self):
        assert _event_type_name(OutputEvent(pane_id="%0", data="hello")) == "output"

    def test_event_type_name_mode_change(self):
        assert _event_type_name(ModeChangeEvent(pane_id="%0")) == "mode_change"

    def test_event_type_name_layout_change(self):
        assert _event_type_name(LayoutChangeEvent(window_id="@0", layout="abc")) == "layout_change"

    def test_event_type_name_window(self):
        assert _event_type_name(WindowEvent(window_id="@0", action="add")) == "window"

    def test_event_type_name_session(self):
        assert _event_type_name(SessionEvent(session_id="$0", name="test")) == "session"

    def test_event_type_name_exit(self):
        assert _event_type_name(ExitEvent(reason="detached")) == "exit"

    def test_event_to_data_output(self):
        event = OutputEvent(pane_id="%0", data="hello world")
        data = _event_to_data(event)
        parsed = json.loads(data)
        assert parsed == {"pane_id": "%0", "data": "hello world"}

    def test_event_to_data_layout_change(self):
        event = LayoutChangeEvent(window_id="@0", layout="main-vertical")
        data = _event_to_data(event)
        parsed = json.loads(data)
        assert parsed == {"window_id": "@0", "layout": "main-vertical"}

    def test_format_sse(self):
        result = format_sse("output", '{"pane_id": "%0", "data": "hi"}')
        assert result == 'event: output\ndata: {"pane_id": "%0", "data": "hi"}\n\n'

    def test_format_sse_error(self):
        result = format_sse("error", '{"message": "stream ended"}')
        assert result.startswith("event: error\n")
        assert result.endswith("\n\n")


class TestParseSSEFrame:
    def test_roundtrip(self):
        """parse_sse_frame inverts format_sse."""
        frame = format_sse("output", '{"pane_id": "%0", "data": "hello"}')
        result = parse_sse_frame(frame)
        assert result is not None
        event_type, data = result
        assert event_type == "output"
        assert json.loads(data) == {"pane_id": "%0", "data": "hello"}

    def test_error_event(self):
        frame = format_sse("error", '{"message": "stream ended"}')
        result = parse_sse_frame(frame)
        assert result == ("error", '{"message": "stream ended"}')

    def test_missing_event_line(self):
        assert parse_sse_frame('data: {"foo": 1}\n\n') is None

    def test_missing_data_line(self):
        assert parse_sse_frame("event: output\n\n") is None

    def test_empty_string(self):
        assert parse_sse_frame("") is None

    def test_garbage(self):
        assert parse_sse_frame("just some random text") is None


def _make_mock_manager(events: list | None = None):
    """Create a mock ControlModeManager with a fake connection.

    If events is provided, the connection's stream() yields those events.
    """
    mock_conn = MagicMock()
    mock_conn.state = "connected"

    if events is not None:

        async def fake_stream():
            for ev in events:
                yield ev

        mock_conn.stream = fake_stream
    else:
        # Indefinite stream that never yields (blocks until cancelled)
        async def blocking_stream():
            try:
                await asyncio.get_event_loop().create_future()
            except asyncio.CancelledError:
                return
            yield  # Make it a generator  # pragma: no cover

        mock_conn.stream = blocking_stream

    manager = MagicMock()
    manager.connect = AsyncMock(return_value=mock_conn)
    manager.disconnect = AsyncMock()
    return manager, mock_conn


@pytest.mark.asyncio
class TestBrokerSubscribe:
    async def test_first_subscribe_creates_connection(self):
        manager, mock_conn = _make_mock_manager()
        config = BackboneConfig()
        broker = StreamBroker(config, manager=manager)

        client_id, queue = await broker.subscribe("test-session")
        assert client_id == 0
        assert broker.client_count("test-session") == 1
        manager.connect.assert_awaited_once_with("test-session", config.control_mode)

        await broker.shutdown()

    async def test_multiple_subscribes_reuse_connection(self):
        manager, mock_conn = _make_mock_manager()
        config = BackboneConfig()
        broker = StreamBroker(config, manager=manager)

        cid1, _ = await broker.subscribe("test-session")
        cid2, _ = await broker.subscribe("test-session")

        assert cid1 == 0
        assert cid2 == 1
        assert broker.client_count("test-session") == 2
        # connect called only once
        assert manager.connect.await_count == 1

        await broker.shutdown()

    async def test_unsubscribe_decrements_count(self):
        manager, _ = _make_mock_manager()
        config = BackboneConfig()
        broker = StreamBroker(config, manager=manager)

        cid1, _ = await broker.subscribe("test-session")
        cid2, _ = await broker.subscribe("test-session")
        assert broker.client_count("test-session") == 2

        await broker.unsubscribe("test-session", cid1)
        assert broker.client_count("test-session") == 1

        await broker.shutdown()

    async def test_client_count_nonexistent_session(self):
        config = BackboneConfig()
        broker = StreamBroker(config)
        assert broker.client_count("nonexistent") == 0


@pytest.mark.asyncio
class TestGracePeriod:
    async def test_last_unsubscribe_triggers_grace_disconnect(self):
        manager, _ = _make_mock_manager()
        config = BackboneConfig(
            control_mode=ControlModeConfig(stream_grace_period_seconds=0)
        )
        broker = StreamBroker(config, manager=manager)

        cid, _ = await broker.subscribe("test-session")
        await broker.unsubscribe("test-session", cid)

        # Give grace task time to run (grace period is 0)
        await asyncio.sleep(0.1)

        manager.disconnect.assert_awaited_once_with("test-session")
        assert broker.client_count("test-session") == 0

    async def test_new_client_cancels_grace_timer(self):
        manager, _ = _make_mock_manager()
        config = BackboneConfig(
            control_mode=ControlModeConfig(stream_grace_period_seconds=10)
        )
        broker = StreamBroker(config, manager=manager)

        cid1, _ = await broker.subscribe("test-session")
        await broker.unsubscribe("test-session", cid1)

        # Re-subscribe before grace period expires
        cid2, _ = await broker.subscribe("test-session")
        assert broker.client_count("test-session") == 1

        # Grace should be cancelled — disconnect not called
        manager.disconnect.assert_not_awaited()

        await broker.shutdown()


@pytest.mark.asyncio
class TestFanOut:
    async def test_events_delivered_to_all_clients(self):
        events = [
            OutputEvent(pane_id="%0", data="hello"),
            OutputEvent(pane_id="%0", data="world"),
        ]
        manager, _ = _make_mock_manager(events)
        config = BackboneConfig()
        broker = StreamBroker(config, manager=manager)

        cid1, q1 = await broker.subscribe("test-session")
        cid2, q2 = await broker.subscribe("test-session")

        # Wait for producer to process all events + send sentinel
        await asyncio.sleep(0.2)

        # Both queues should have the same events
        q1_items = []
        while not q1.empty():
            q1_items.append(q1.get_nowait())

        q2_items = []
        while not q2.empty():
            q2_items.append(q2.get_nowait())

        # Both should have: 2 output frames + 1 error frame + 1 None sentinel
        assert len(q1_items) >= 3  # at least 2 events + error
        assert len(q2_items) >= 3

        # First two frames should be output events
        for items in [q1_items, q2_items]:
            assert "event: output" in items[0]
            assert '"hello"' in items[0]
            assert "event: output" in items[1]
            assert '"world"' in items[1]

        await broker.shutdown()

    async def test_stream_end_sends_sentinel(self):
        events = [OutputEvent(pane_id="%0", data="done")]
        manager, _ = _make_mock_manager(events)
        config = BackboneConfig()
        broker = StreamBroker(config, manager=manager)

        cid, queue = await broker.subscribe("test-session")

        # Wait for producer to finish
        await asyncio.sleep(0.2)

        items = []
        while not queue.empty():
            items.append(queue.get_nowait())

        # Last item should be None sentinel
        assert items[-1] is None
        # Second to last should be error frame
        assert "event: error" in items[-2]

        await broker.shutdown()


@pytest.mark.asyncio
class TestShutdown:
    async def test_shutdown_tears_down_all_sessions(self):
        manager, _ = _make_mock_manager()
        config = BackboneConfig()
        broker = StreamBroker(config, manager=manager)

        await broker.subscribe("session-a")
        await broker.subscribe("session-b")

        await broker.shutdown()

        assert manager.disconnect.await_count == 2
        assert broker.client_count("session-a") == 0
        assert broker.client_count("session-b") == 0
