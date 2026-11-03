"""The Socket.IO ``/terminal`` namespace streams registered agents only."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.api.socketio_server import TerminalNamespace

_MOD = "agent_backbone.api.socketio_server"


async def test_resize_reattaches_after_concurrent_terminal_collapse():
    from agent_backbone.services.terminal import PtyManager, PtySession

    manager = PtyManager()
    ns = TerminalNamespace("/terminal")
    ns.emit = AsyncMock()
    ns._profiles["sid"] = None
    ns._subscriptions["sid"] = {"app": None}
    entered, release = asyncio.Event(), asyncio.Event()
    with (
        patch.object(PtySession, "start"),
        patch(f"{_MOD}.get_pty_manager", return_value=manager),
        patch(f"{_MOD}.resize_window", new_callable=AsyncMock, return_value=True),
        patch(f"{_MOD}._restore_dynamic_window_size", new_callable=AsyncMock),
    ):
        await ns._attach_subscription_client("sid", "app", 80, 24)
        old = manager.get("sid", "app")
        cleanup = old._cleanup

        async def paused_cleanup():
            entered.set()
            await release.wait()
            await cleanup()

        with patch.object(old, "_cleanup", side_effect=paused_cleanup):
            collapse = asyncio.create_task(ns.on_release_dims("sid", {"session": "app"}))
            resize = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                resize = asyncio.create_task(
                    ns.on_resize("sid", {"session": "app", "cols": 80, "rows": 24})
                )
                await asyncio.sleep(0)
                assert not resize.done()
                release.set()
                await asyncio.gather(collapse, resize)
                assert manager.get("sid", "app") not in (None, old)
                assert isinstance(ns._subscriptions["sid"]["app"], asyncio.Task)
            finally:
                release.set()
                await asyncio.gather(
                    collapse, *([resize] if resize else []), return_exceptions=True
                )
                forwarding = [t for t in ns._subscriptions.get("sid", {}).values() if t]
                for task in forwarding:
                    task.cancel()
                await asyncio.gather(*forwarding, return_exceptions=True)
                await manager.cleanup_all()


def _namespace(config) -> TerminalNamespace:
    ns = TerminalNamespace("/terminal")
    ns.server = SimpleNamespace(fastapi_app=SimpleNamespace(state=SimpleNamespace(config=config)))
    ns.emit = AsyncMock()
    ns._attach_subscription_client = AsyncMock()
    ns._profiles["sid1"] = None
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
        ns._attach_subscription_client.assert_awaited_once_with(
            "sid1", "ike", 80, 24, join_room=True
        )
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


async def test_disconnect_cleanup_failure_still_removes_scoped_reader(api_app):
    sio, ns, sid, spec = await _scoped_connection(api_app, "/terminal")
    ns._subscriptions[sid] = {spec.name: None, "another": None}
    with patch.object(
        ns, "_cleanup_session", side_effect=[RuntimeError("detach failed"), None]
    ) as cleanup:
        await sio.disconnect(sid, namespace=ns.namespace)
    assert cleanup.await_count == 2
    assert sid in ns._subscriptions and sid not in ns._registrations
    assert not list(sio.manager.get_participants(ns.namespace, None))
    await sio.emit("terminal_output", "must not be sent", namespace=ns.namespace)
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


async def _operator_connection(sio, ns, api_app):
    sid = await sio.manager.connect("operator-engine", ns.namespace)
    assert await ns.on_connect(sid, {}, {"api_key": api_app.state.config.api_key})
    return sid


async def test_targeted_output_checks_only_its_recipient(api_app):
    from agent_backbone.config import agent_api_key

    sio, ns, sid, spec = await _scoped_connection(api_app, "/terminal")
    other = await sio.manager.connect("other-engine", ns.namespace)
    assert await ns.on_connect(
        other,
        {},
        {
            "api_key": agent_api_key(api_app.state.config.api_key, spec.registration_id),
        },
    )
    operator = await _operator_connection(sio, ns, api_app)
    repo = api_app.state.db.agents
    with patch.object(repo, "registration_name", wraps=repo.registration_name) as lookup:
        await sio.emit("terminal_output", "one", to=sid, namespace=ns.namespace)
        lookup.assert_awaited_once_with(spec.registration_id)
        lookup.reset_mock()
        await sio.emit("terminal_output", "two", to=operator, namespace=ns.namespace)
        lookup.assert_not_awaited()
    sent = [call.args for call in sio._send_eio_packet.await_args_list]
    assert any(eio_sid == "engine" and "one" in packet.data for eio_sid, packet in sent)
    assert not any(eio_sid == "engine" and "two" in packet.data for eio_sid, packet in sent)


async def test_registration_lookup_failure_excludes_scoped_reader_without_failing_feed(api_app):
    from agent_backbone.api.session_updates import SessionFeed

    sio, ns, sid, _ = await _scoped_connection(api_app, "/sessions")
    operator = await _operator_connection(sio, ns, api_app)
    feed = SessionFeed(lambda: api_app.state.config, sio)
    with (
        patch.object(feed, "snapshot", new_callable=AsyncMock, return_value=[]),
        patch.object(
            api_app.state.db.agents, "registration_name", side_effect=RuntimeError("busy")
        ),
    ):
        assert await feed.emit()
    assert not sio.manager.is_connected(sid, ns.namespace)
    assert sio.manager.is_connected(operator, ns.namespace)
    sio._send_eio_packet.assert_awaited_once()
    assert sio._send_eio_packet.await_args.args[0] == "operator-engine"


async def test_retired_reader_is_excluded_even_when_disconnect_is_still_pending(api_app):
    sio, ns, sid, spec = await _scoped_connection(api_app, "/sessions")
    await api_app.state.db.agents.delete(spec.name)
    with patch.object(ns, "disconnect", new_callable=AsyncMock) as disconnect:
        await sio.emit("sessions:update", [], namespace=ns.namespace)
    disconnect.assert_awaited_once_with(sid)
    sio._send_eio_packet.assert_not_awaited()


async def test_concurrent_collapse_finishes_after_forwarder_disconnect_swallows_cancel(api_app):
    from agent_backbone.services.terminal import PtyManager, PtySession

    sio, ns, sid, spec = await _scoped_connection(api_app, "/terminal")
    manager = PtyManager()
    locked, disconnecting, allow_detach = (asyncio.Event() for _ in range(3))
    cleanup, detach = ns._cleanup_session, ns._detach_locked

    async def observe_disconnect(*args):
        disconnecting.set()
        await cleanup(*args)

    async def hold_detach(*args):
        locked.set()
        await allow_detach.wait()
        await detach(*args)

    with (
        patch.object(PtySession, "start"),
        patch(f"{_MOD}.get_pty_manager", return_value=manager),
    ):
        await ns._attach_subscription_client(sid, spec.name, 80, 24)
        pty = manager.get(sid, spec.name)
        forwarding = ns._subscriptions[sid][spec.name]
        await api_app.state.db.agents.delete(spec.name)
        with (
            patch.object(ns, "_detach_locked", side_effect=hold_detach),
            patch.object(ns, "_cleanup_session", side_effect=observe_disconnect),
        ):
            collapse = asyncio.create_task(ns.on_release_dims(sid, {"session": spec.name}))
            try:
                await asyncio.wait_for(locked.wait(), 2)
                pty.output_queue.put_nowait("retired output")
                await asyncio.wait_for(disconnecting.wait(), 2)
                assert len(ns._resource_tasks[(sid, spec.name)]) == 2
                # Collapse now cancels a forwarder awaiting disconnect cleanup
                # behind its lock. AsyncNamespace swallows that cancellation.
                allow_detach.set()
                await asyncio.wait_for(asyncio.shield(collapse), 2)
                assert forwarding.done()
                assert manager.get(sid, spec.name) is None
                assert not ns._resources
            finally:
                allow_detach.set()
                pty.output_queue.put_nowait(None)
                await asyncio.gather(forwarding, collapse, return_exceptions=True)
                await asyncio.gather(*tuple(ns._background), return_exceptions=True)
                await ns.on_disconnect(sid)
                await manager.cleanup_all()


@pytest.mark.parametrize("cancel_at", ["queued", "created"])
async def test_cancelled_attach_retires_worker_result_before_caller_resumes(cancel_at):
    from agent_backbone.services.terminal import PtyManager, PtySession

    ns = TerminalNamespace("/terminal")
    ns._profiles["sid"] = None
    ns.emit = AsyncMock()
    ns.leave_room = AsyncMock()
    manager = PtyManager()
    create, remove = manager.create, manager.remove
    removed = asyncio.Event()
    request = None

    async def observe_remove(*args):
        await remove(*args)
        removed.set()

    async def create_then_cancel(*args):
        result = await create(*args)
        if cancel_at == "created":
            # Queue cancellation before the worker's result wakes its caller.
            asyncio.get_running_loop().call_soon(request.cancel)
        return result

    with (
        patch.object(PtySession, "start"),
        patch.object(manager, "create", side_effect=create_then_cancel),
        patch.object(manager, "remove", side_effect=observe_remove),
        patch(f"{_MOD}.get_pty_manager", return_value=manager),
    ):
        request = asyncio.create_task(ns._attach_subscription_client("sid", "app", 80, 24))
        try:
            if cancel_at == "queued":
                await asyncio.sleep(0)  # Worker is queued ahead of the cancelled caller.
                request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            if cancel_at == "created":
                await asyncio.wait_for(removed.wait(), 2)
            while ns._resource_tasks or ns._background:
                await asyncio.gather(
                    *(t for pending in tuple(ns._resource_tasks.values()) for t in tuple(pending)),
                    *tuple(ns._background),
                    return_exceptions=True,
                )
            assert not manager._sessions
            assert not ns._resources
            assert ns._subscriptions.get("sid", {}).get("app") is None
        finally:
            await ns.on_disconnect("sid")
            await asyncio.gather(*tuple(ns._background), return_exceptions=True)
            await manager.cleanup_all()


@pytest.mark.parametrize("stage", ["queued", "fence"])
@pytest.mark.parametrize("control", ["release_dims", "resize"])
async def test_terminal_controls_wait_for_pending_attach(stage, control):
    from contextlib import asynccontextmanager

    from agent_backbone.services.terminal import PtyManager, PtySession

    entered, allow = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def operation():
        entered.set()
        await allow.wait()
        yield

    ns = TerminalNamespace("/terminal")
    ns._profiles["sid"] = SimpleNamespace(operation=operation) if stage == "fence" else None
    ns.emit = AsyncMock()
    ns.leave_room = AsyncMock()
    manager = PtyManager()
    with (
        patch.object(PtySession, "start"),
        patch.object(PtySession, "resize") as resize,
        patch(f"{_MOD}.resize_window", new_callable=AsyncMock, return_value=True),
        patch(f"{_MOD}._restore_dynamic_window_size", new_callable=AsyncMock),
        patch(f"{_MOD}.get_pty_manager", return_value=manager),
    ):
        attach = asyncio.create_task(ns._attach_subscription_client("sid", "app", 80, 24))
        try:
            if stage == "fence":
                await asyncio.wait_for(entered.wait(), 2)
            else:
                await asyncio.sleep(0)  # Request queued its independent worker.
            assert "app" not in ns._subscriptions.get("sid", {})
            asyncio.get_running_loop().call_soon(allow.set)
            await getattr(ns, f"on_{control}")("sid", {"session": "app", "cols": 120, "rows": 40})
            await attach
            if control == "release_dims":
                assert not manager._sessions
                assert not ns._resources
            else:
                assert manager.get("sid", "app") is not None
                resize.assert_called_once_with(120, 40)
            ns.emit.assert_not_awaited()
        finally:
            allow.set()
            await attach
            await ns.on_disconnect("sid")
            await asyncio.gather(*tuple(ns._background), return_exceptions=True)
            await manager.cleanup_all()


async def test_failed_join_cannot_remove_a_successful_overlapping_join(config):
    from agent_backbone.services.terminal import PtyManager, PtySession

    ns = TerminalNamespace("/terminal")
    ns.server = SimpleNamespace(fastapi_app=SimpleNamespace(state=SimpleNamespace(config=config)))
    ns._profiles["sid"] = None
    ns.emit = AsyncMock()
    rooms = set()

    async def enter(sid, room):
        rooms.add((sid, room))

    async def leave(sid, room):
        rooms.discard((sid, room))

    ns.enter_room, ns.leave_room = enter, leave
    manager = PtyManager()
    with (
        patch.object(PtySession, "start", side_effect=[OSError("spawn failed"), None]),
        patch(f"{_MOD}.get_pty_manager", return_value=manager),
        patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True),
    ):
        joins = [
            asyncio.create_task(ns.on_join("sid", {"session": "ike", "cols": 80, "rows": 24}))
            for _ in range(2)
        ]
        try:
            results = await asyncio.gather(*joins, return_exceptions=True)
            assert isinstance(results[0], OSError)
            assert results[1] is None
            assert manager.get("sid", "ike") is not None
            assert isinstance(ns._subscriptions["sid"]["ike"], asyncio.Task)
            assert ("sid", "session:ike") in rooms
        finally:
            await ns.on_disconnect("sid")
            await asyncio.gather(*tuple(ns._background), return_exceptions=True)
            await manager.cleanup_all()
