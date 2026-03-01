"""Tests for api/routes/stream.py — SSE endpoint."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from agent_backbone.services.streaming import StreamBroker
from api.deps import get_tmux_service
from api.routes.stream import get_broker


@pytest.fixture
def _reset_broker():
    """Reset the module-level broker singleton between tests."""
    from api.broker import reset_broker

    reset_broker()
    yield
    reset_broker()


def _mock_broker(subscribe_side_effect=None, subscribe_return=None):
    """Create a mock StreamBroker."""
    broker = MagicMock(spec=StreamBroker)
    broker.subscribe = AsyncMock(
        side_effect=subscribe_side_effect,
        return_value=subscribe_return,
    )
    broker.unsubscribe = AsyncMock()
    broker.shutdown = AsyncMock()
    return broker


def _mock_tmux_svc(session_exists_result=True):
    """Create a mock TmuxService."""
    svc = MagicMock()
    svc.session_exists = AsyncMock(return_value=session_exists_result)
    return svc


@pytest.mark.asyncio
class TestStreamEndpoint:
    async def test_404_nonexistent_session(self, api_app, api_key, auth_headers, _reset_broker):
        """Returns 404 when session doesn't exist."""
        mock_broker = _mock_broker()
        api_app.dependency_overrides[get_broker] = lambda: mock_broker
        api_app.dependency_overrides[get_tmux_service] = lambda: _mock_tmux_svc(
            session_exists_result=False
        )
        try:
            transport = ASGITransport(app=api_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/agents/nonexistent/stream",
                    headers=auth_headers,
                )
                assert resp.status_code == 404
                assert "not found" in resp.json()["detail"]
        finally:
            api_app.dependency_overrides.pop(get_broker, None)
            api_app.dependency_overrides.pop(get_tmux_service, None)

    async def test_502_connection_failure(self, api_app, api_key, auth_headers, _reset_broker):
        """Returns 502 when control mode connection fails."""
        mock_broker = _mock_broker(subscribe_side_effect=RuntimeError("connection failed"))
        api_app.dependency_overrides[get_broker] = lambda: mock_broker
        api_app.dependency_overrides[get_tmux_service] = lambda: _mock_tmux_svc(
            session_exists_result=True
        )
        try:
            transport = ASGITransport(app=api_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/agents/test-session/stream",
                    headers=auth_headers,
                )
                assert resp.status_code == 502
                assert "control mode" in resp.json()["detail"].lower()
        finally:
            api_app.dependency_overrides.pop(get_broker, None)
            api_app.dependency_overrides.pop(get_tmux_service, None)

    async def test_401_without_api_key(self, api_app, api_key, _reset_broker):
        """Returns 401 without Bearer token when API key is set."""
        transport = ASGITransport(app=api_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/agents/test-session/stream")
            assert resp.status_code == 401

    async def test_sse_event_format(self, api_app, api_key, auth_headers, _reset_broker):
        """Streams SSE events with correct format."""
        events_queue: asyncio.Queue[str | None] = asyncio.Queue()

        # Pre-fill the queue with events
        sse_frame = f"event: output\ndata: {json.dumps({'pane_id': '%0', 'data': 'hello'})}\n\n"
        await events_queue.put(sse_frame)
        await events_queue.put(None)  # Sentinel to end stream

        mock_broker = _mock_broker(subscribe_return=(0, events_queue))
        api_app.dependency_overrides[get_broker] = lambda: mock_broker
        api_app.dependency_overrides[get_tmux_service] = lambda: _mock_tmux_svc(
            session_exists_result=True
        )
        try:
            transport = ASGITransport(app=api_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                async with client.stream(
                    "GET",
                    "/api/agents/test-session/stream",
                    headers=auth_headers,
                ) as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
                    assert resp.headers["cache-control"] == "no-cache"
                    assert resp.headers["x-accel-buffering"] == "no"

                    body = ""
                    async for chunk in resp.aiter_text():
                        body += chunk

                    assert "event: output" in body
                    assert '"pane_id": "%0"' in body
                    assert '"data": "hello"' in body

            # Verify unsubscribe was called on stream end
            mock_broker.unsubscribe.assert_awaited_once_with("test-session", 0)
        finally:
            api_app.dependency_overrides.pop(get_broker, None)
            api_app.dependency_overrides.pop(get_tmux_service, None)
