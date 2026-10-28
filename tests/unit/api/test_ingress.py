"""Requests are bounded before parsing, including unauthenticated transports."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_backbone.api.ingress import IngressMiddleware
from agent_backbone.api.signed import MAX_BODY_BYTES
from agent_backbone.config import SecurityConfig


@pytest.mark.parametrize("path", ["/api/messages", "/webhook/github", "/socket.io/"])
async def test_chunked_overflow_stops_before_downstream(config, path):
    app, send = AsyncMock(), AsyncMock()
    receive = AsyncMock(
        side_effect=[
            {"type": "http.request", "body": b"a" * MAX_BODY_BYTES, "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": True},
        ]
    )
    guard = IngressMiddleware(app, SimpleNamespace(state=SimpleNamespace(config=config)))
    await guard({"type": "http", "path": path, "headers": []}, receive, send)
    app.assert_not_called()
    assert send.call_args_list[0].args[0]["status"] == 413
    assert receive.await_count == 2


async def test_declared_overflow_never_reads_body(config):
    app, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    guard = IngressMiddleware(app, SimpleNamespace(state=SimpleNamespace(config=config)))
    await guard({"type": "http", "headers": [(b"content-length", b"999999999")]}, receive, send)
    receive.assert_not_called()
    app.assert_not_called()
    assert send.call_args_list[0].args[0]["status"] == 413


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize(
    "host,origin,allowed",
    [
        ("localhost:7120", "http://localhost:7120", True),
        ("127.0.0.1:7120", None, True),
        ("attacker.invalid:7120", "http://attacker.invalid:7120", False),
        ("localhost:7120", "https://attacker.invalid", False),
        ("localhost:7120", "null", False),
    ],
)
async def test_keyless_host_and_origin_guard(config, transport, host, origin, allowed):
    config = replace(config, api_key="", security=SecurityConfig(allow_unauthenticated=True))
    app, send = AsyncMock(), AsyncMock()
    receive = AsyncMock(return_value={"type": "http.request", "body": b"{}"})
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    guard = IngressMiddleware(app, SimpleNamespace(state=SimpleNamespace(config=config)))
    await guard({"type": transport, "scheme": "http", "headers": headers}, receive, send)
    assert app.await_count == int(allowed)
    if not allowed:
        receive.assert_not_called()
        assert send.call_args_list[0].args[0].get("status", 403) == 403


@pytest.mark.parametrize("prefix", ["", "/bb"])
async def test_document_limit_and_body_replay(config, prefix):
    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())

    body = b"x" * (MAX_BODY_BYTES + 1)
    guard = IngressMiddleware(app, SimpleNamespace(state=SimpleNamespace(config=config)))
    await guard(
        {"type": "http", "path": prefix + "/api/documents", "root_path": prefix, "headers": []},
        AsyncMock(return_value={"type": "http.request", "body": body}),
        AsyncMock(),
    )
    assert seen == [{"type": "http.request", "body": body, "more_body": False}]


async def test_disconnect_before_final_body_does_not_dispatch(config):
    app, send = AsyncMock(), AsyncMock()
    receive = AsyncMock(
        side_effect=[
            {"type": "http.request", "body": b"{}", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    guard = IngressMiddleware(app, SimpleNamespace(state=SimpleNamespace(config=config)))
    await guard({"type": "http", "headers": []}, receive, send)
    app.assert_not_called()
    send.assert_not_called()


@pytest.mark.parametrize("declared", [True, False])
async def test_size_refusal_keeps_configured_cors_headers(config, declared):
    origin = "https://console.example"
    config = replace(config, backbone=replace(config.backbone, cors_origins=(origin,)))
    send = AsyncMock()
    headers = [(b"origin", origin.encode())]
    if declared:
        headers.append((b"content-length", b"999999999"))
    guard = IngressMiddleware(AsyncMock(), SimpleNamespace(state=SimpleNamespace(config=config)))
    await guard(
        {"type": "http", "method": "POST", "headers": headers},
        AsyncMock(return_value={"type": "http.request", "body": b"x" * (MAX_BODY_BYTES + 1)}),
        send,
    )
    response = send.call_args_list[0].args[0]
    assert response["status"] == 413
    assert (b"access-control-allow-origin", origin.encode()) in response["headers"]


async def test_cors_exposes_sheet_export_warning_header(config):
    from httpx import ASGITransport, AsyncClient

    from agent_backbone.api.app import create_app

    origin = "https://console.example"
    config = replace(config, backbone=replace(config.backbone, cors_origins=(origin,)))
    transport = ASGITransport(app=create_app(config).other_asgi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/sheets", headers={"Origin": origin})
    assert "x-backbone-sheet-warnings" in response.headers["access-control-expose-headers"].lower()
