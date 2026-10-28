"""The Socket.IO ``/terminal`` namespace streams registered agents only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.api.socketio_server import TerminalNamespace

_MOD = "agent_backbone.api.socketio_server"


def _namespace(config) -> TerminalNamespace:
    ns = TerminalNamespace("/terminal")
    ns.server = SimpleNamespace(fastapi_app=SimpleNamespace(state=SimpleNamespace(config=config)))
    ns.emit = AsyncMock()
    ns._attach_subscription_client = AsyncMock()
    ns.enter_room = AsyncMock()
    return ns


class TestTerminalJoin:
    async def test_unregistered_tmux_session_is_refused(self, config):
        ns = _namespace(config)
        with patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True) as ex:
            await ns.on_join("sid1", {"session": "stray", "cols": 80, "rows": 24})
        ns.emit.assert_awaited_once()
        assert "not a registered agent" in ns.emit.await_args.args[1]["message"]
        ns._attach_subscription_client.assert_not_awaited()
        ex.assert_not_awaited()  # refused before touching tmux

    async def test_non_string_session_is_rejected_cleanly(self, config):
        # A JSON array/object as "session" must not reach the registry lookup
        # (unhashable → TypeError); it is a malformed request, nothing more.
        ns = _namespace(config)
        for bad in (["ike"], {"name": "ike"}, 7):
            await ns.on_join("sid1", {"session": bad})
        assert ns.emit.await_count == 3
        assert all("Missing session name" in c.args[1]["message"] for c in ns.emit.await_args_list)
        ns._attach_subscription_client.assert_not_called()

    async def test_registered_agent_is_streamed(self, config):
        ns = _namespace(config)
        with patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True):
            await ns.on_join("sid1", {"session": "ike", "cols": 80, "rows": 24})
        ns._attach_subscription_client.assert_awaited_once_with("sid1", "ike", 80, 24)
        ns.emit.assert_not_awaited()


class TestTerminalMalformedPayloads:
    async def test_non_dict_payloads_never_raise(self, config):
        # Socket.IO payloads are client input: null, a string or a list must
        # be ignored, never an AttributeError that leaks a subscription.
        ns = _namespace(config)
        for bad in (None, "ike", ["ike"], 7):
            await ns.on_leave("sid1", bad)
            await ns.on_resize("sid1", bad)
            await ns.on_release_dims("sid1", bad)
            await ns.on_pause("sid1", bad)
            await ns.on_resume("sid1", bad)
        ns.emit.assert_not_awaited()
        ns._attach_subscription_client.assert_not_awaited()
        assert ns._subscriptions == {}

    async def test_non_string_session_is_ignored(self, config):
        ns = _namespace(config)
        for bad in ({"session": ["ike"]}, {"session": 7}, {"session": None}):
            await ns.on_leave("sid1", bad)
            await ns.on_resize("sid1", bad)
            await ns.on_release_dims("sid1", bad)
            await ns.on_pause("sid1", bad)
            await ns.on_resume("sid1", bad)
        ns.emit.assert_not_awaited()
        ns._attach_subscription_client.assert_not_awaited()
        assert ns._subscriptions == {}


async def test_registration_credential_socket_connect_requires_current_registration(api_app):
    from agent_backbone.config import agent_api_key

    spec = next(iter(api_app.state.agent_store.agents))
    ns = _namespace(api_app.state.config)
    ns.server.fastapi_app = api_app
    auth = {"api_key": agent_api_key(api_app.state.config.api_key, spec.registration_id)}
    assert await ns.on_connect("sid1", {}, auth)
    await api_app.state.db.agents.delete(spec.name)
    assert not await ns.on_connect("sid2", {}, auth)


async def _scoped_connection(api_app, namespace):
    from agent_backbone.api.socketio_server import create_sio
    from agent_backbone.config import agent_api_key

    sio = create_sio([])
    sio.fastapi_app = SimpleNamespace(
        state=SimpleNamespace(config=api_app.state.config, db=api_app.state.db)
    )
    sio._send_packet = AsyncMock()
    sio._send_eio_packet = AsyncMock()
    sid = await sio.manager.connect("engine", namespace)
    ns = sio.namespace_handlers[namespace]
    spec = next(iter(api_app.state.agent_store.agents))
    auth = {"api_key": agent_api_key(api_app.state.config.api_key, spec.registration_id)}
    assert await ns.on_connect(sid, {}, auth)
    return sio, ns, sid, spec


async def test_retired_socket_cannot_join_another_terminal(api_app):
    sio, ns, sid, spec = await _scoped_connection(api_app, "/terminal")
    await api_app.state.db.agents.delete(spec.name)
    with patch.object(ns, "_attach_subscription_client", new_callable=AsyncMock) as attach:
        await ns.trigger_event("join", sid, {"session": "bell"})
    attach.assert_not_awaited()
    assert not sio.manager.is_connected(sid, "/terminal")
    assert sid not in ns._registrations


async def test_retirement_stops_existing_terminal_stream_without_emitting_its_data(api_app):
    import asyncio

    sio, ns, sid, spec = await _scoped_connection(api_app, "/terminal")
    queue = asyncio.Queue()
    task = asyncio.create_task(ns._forward_pty_output(sid, spec.name, queue))
    ns._subscriptions[sid] = {spec.name: task}
    manager = SimpleNamespace(remove=AsyncMock(side_effect=lambda *_: queue.put_nowait(None)))
    with patch(f"{_MOD}.get_pty_manager", return_value=manager):
        await api_app.state.db.agents.delete(spec.name)
        await queue.put("must not be sent")
        await asyncio.wait_for(task, timeout=1)
    manager.remove.assert_awaited_once_with(sid, spec.name)
    assert not sio.manager.is_connected(sid, "/terminal")
    assert sid not in ns._subscriptions and sid not in ns._registrations
    # Only the namespace disconnect packet is sent, never terminal output.
    assert all(call.args[1].packet_type == 1 for call in sio._send_packet.await_args_list)
    sio._send_eio_packet.assert_not_awaited()


async def test_retirement_removes_session_feed_subscriber_before_next_broadcast(api_app):
    from agent_backbone.api.session_updates import SessionFeed

    sio, ns, sid, spec = await _scoped_connection(api_app, "/sessions")
    feed = SessionFeed(lambda: api_app.state.config, sio)
    await api_app.state.db.agents.delete(spec.name)
    with patch.object(feed, "snapshot", new_callable=AsyncMock, return_value=[]):
        assert await feed.emit()
    assert not sio.manager.is_connected(sid, "/sessions")
    assert sid not in ns._registrations
    assert all(call.args[1].packet_type == 1 for call in sio._send_packet.await_args_list)
    sio._send_eio_packet.assert_not_awaited()


@pytest.mark.parametrize("credential", ["shared", "operator"])
async def test_retirement_preserves_existing_shared_socket_authority(api_app, credential):
    from agent_backbone.api.socketio_server import create_sio
    from agent_backbone.config import agent_api_key

    sio = create_sio([])
    sio.fastapi_app = SimpleNamespace(
        state=SimpleNamespace(config=api_app.state.config, db=api_app.state.db)
    )
    sio._send_packet = AsyncMock()
    sio._send_eio_packet = AsyncMock()
    sid = await sio.manager.connect("engine", "/sessions")
    ns = sio.namespace_handlers["/sessions"]
    key = api_app.state.config.api_key
    if credential == "shared":
        key = agent_api_key(key)
    assert await ns.on_connect(sid, {}, {"api_key": key})
    spec = next(iter(api_app.state.agent_store.agents))
    await api_app.state.db.agents.delete(spec.name)
    await sio.emit("sessions:update", [], namespace="/sessions")
    assert sio.manager.is_connected(sid, "/sessions")
    sio._send_eio_packet.assert_awaited_once()
    assert "sessions:update" in sio._send_eio_packet.await_args.args[1].data
