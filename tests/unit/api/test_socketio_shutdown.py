"""Socket shutdown owns detached work until its dependencies may be disposed."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.api.app import _SocketStreams
from agent_backbone.api.socketio_server import create_sio
from agent_backbone.base import LifecycleManager
from agent_backbone.services.terminal import PtyManager, PtySession

_MOD = "agent_backbone.api.socketio_server"


async def test_shutdown_waits_for_admitted_connect_but_not_its_transport_task(api_app):
    sio = create_sio([])
    sio.fastapi_app = api_app
    ns = sio.namespace_handlers["/sessions"]
    entered, release, transport_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    result = []

    async def connected(*args):
        entered.set()
        await release.wait()

    async def transport():
        result.append(
            await ns.trigger_event("connect", "sid", {}, {"api_key": api_app.state.config.api_key})
        )
        await transport_done.wait()

    with patch.object(ns, "_connected", side_effect=connected):
        connection = asyncio.create_task(transport())
        shutdown = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            shutdown = asyncio.create_task(sio.shutdown())
            await asyncio.sleep(0)
            assert not shutdown.done()
            assert await ns.trigger_event("connect", "later", {}, {}) is False
            release.set()
            await asyncio.wait_for(shutdown, 2)
            assert result == [False]
            assert not connection.done()
            assert not ns._profiles
        finally:
            release.set()
            transport_done.set()
            await asyncio.gather(
                connection, *([shutdown] if shutdown else []), return_exceptions=True
            )


async def test_shutdown_drains_scheduled_handlers_and_rejects_later_events():
    sio = create_sio([])
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(*args):
        entered.set()
        await release.wait()

    with (
        patch.object(sio.manager, "is_connected", return_value=True),
        patch.object(sio.manager, "sid_from_eio_sid", return_value="sid"),
        patch.object(sio, "_handle_event_internal", side_effect=handler) as handle,
    ):
        await sio._handle_event("transport", "/terminal", 1, ["join", {}])
        await asyncio.wait_for(entered.wait(), 2)
        shutdown = asyncio.create_task(sio.shutdown())
        try:
            await asyncio.sleep(0)
            await sio._handle_event("transport", "/terminal", 2, ["join", {}])
            assert not shutdown.done()
            assert handle.call_count == 1
        finally:
            release.set()
            await asyncio.wait_for(shutdown, 2)
        assert not sio._handlers


@pytest.mark.parametrize("profile", [False, True])
async def test_shutdown_drains_emissions_despite_shutdown_caller_cancellation(profile):
    sio = create_sio([])
    entered, release = asyncio.Event(), asyncio.Event()

    async def emit(*args):
        entered.set()
        await release.wait()

    sio.fastapi_app = SimpleNamespace(
        state=SimpleNamespace(profile_provider=object() if profile else None)
    )
    with patch.object(sio, "_emit", side_effect=emit) as sending:
        caller = asyncio.create_task(sio.emit("test"))
        await asyncio.wait_for(entered.wait(), 2)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        shutdown = asyncio.create_task(sio.shutdown())
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.gather(shutdown, return_exceptions=True)
        retry = asyncio.create_task(sio.shutdown())
        try:
            await asyncio.sleep(0)
            assert not retry.done()
            await sio.emit("late")
            assert sending.call_count == 1
        finally:
            release.set()
            await asyncio.wait_for(retry, 2)
        await asyncio.gather(caller, return_exceptions=True)
        assert not sio._profile_emissions


async def test_shutdown_cleans_retained_resources_without_connected_sids():
    sio = create_sio([])
    ns = sio.namespace_handlers["/terminal"]
    manager = PtyManager()
    ns._profiles["sid"] = None
    with (
        patch.object(PtySession, "start"),
        patch(f"{_MOD}.get_pty_manager", return_value=manager),
        patch(f"{_MOD}.resize_window", new_callable=AsyncMock, return_value=True),
        patch(f"{_MOD}._restore_dynamic_window_size", new_callable=AsyncMock),
    ):
        for name in ("first", "second"):
            await ns._attach_subscription_client("sid", name, 80, 24)
        # A failed/cancelled disconnect has removed admission, but attachments
        # and a collapsed subscription can remain outside the connected roster.
        ns._profiles.clear()
        ns._subscriptions["other"] = {"collapsed": None}
        failed = manager.get("sid", "first")
        with patch.object(failed, "cleanup", side_effect=OSError("busy")):
            with pytest.raises(ExceptionGroup, match="Terminal shutdown cleanup failed"):
                await asyncio.wait_for(sio.shutdown(), 2)
            assert ("sid", "first") in ns._resources
            assert ("sid", "first") in manager._sessions
            assert ("sid", "second") not in manager._sessions
            assert "other" not in ns._subscriptions
        await asyncio.wait_for(sio.shutdown(), 2)
        assert not ns._resources
        assert not ns._subscriptions
        assert not ns._resource_tasks
        assert not ns._background
        assert not manager._sessions


async def test_shutdown_waits_for_pending_attach_and_data_drop_work():
    sio = create_sio([])
    ns = sio.namespace_handlers["/terminal"]
    manager = PtyManager()
    ns._profiles["sid"] = None
    entered, release, drop_entered, drop_release = (asyncio.Event() for _ in range(4))
    create = manager.create

    async def paused_create(*args):
        entered.set()
        await release.wait()
        return await create(*args)

    async def dropped(*args, **kwargs):
        drop_entered.set()
        await drop_release.wait()

    with (
        patch.object(PtySession, "start"),
        patch.object(manager, "create", side_effect=paused_create),
        patch(f"{_MOD}.get_pty_manager", return_value=manager),
        patch.object(ns, "emit", side_effect=dropped),
    ):
        attach = asyncio.create_task(ns._attach_subscription_client("sid", "app", 80, 24))
        await asyncio.wait_for(entered.wait(), 2)
        ns._on_data_dropped("sid", "app")
        await asyncio.wait_for(drop_entered.wait(), 2)
        attach.cancel()
        await asyncio.gather(attach, return_exceptions=True)
        shutdown = asyncio.create_task(sio.shutdown())
        try:
            await asyncio.sleep(0)
            assert not shutdown.done()
            release.set()
            # Creation can retire while pre-existing data-drop work remains.
            await asyncio.sleep(0)
            assert not shutdown.done()
        finally:
            release.set()
            drop_release.set()
            await asyncio.wait_for(shutdown, 2)
        assert not manager._sessions
        assert not ns._resources
        assert not ns._background


async def test_failed_stream_shutdown_retains_dependencies_and_retries_in_order():
    lifecycle = LifecycleManager()
    order = []
    database, producer = AsyncMock(), AsyncMock()
    database.stop.side_effect = lambda: order.append("database")
    producer.stop.side_effect = lambda: order.append("producer")
    sio, feed, manager = AsyncMock(), AsyncMock(), AsyncMock()
    sio.shutdown.side_effect = RuntimeError("retained resource")
    streams = _SocketStreams(sio, feed, manager)
    lifecycle.register("database", database)
    lifecycle.register("streams", streams)
    lifecycle.register("producer", producer)
    await lifecycle.start_all()
    with pytest.raises(RuntimeError, match="retained resource"):
        await lifecycle.stop_all()
    assert order == ["producer"]
    feed.close.assert_not_awaited()
    manager.cleanup_all.assert_not_awaited()
    sio.shutdown.side_effect = lambda: order.append("sockets")
    feed.close.side_effect = lambda: order.append("feed")
    manager.cleanup_all.side_effect = lambda: order.append("pty")
    await lifecycle.stop_all()
    assert order == ["producer", "sockets", "feed", "pty", "database"]
    producer.stop.assert_awaited_once()
