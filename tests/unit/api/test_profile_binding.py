"""Legacy providers must not silently accept an operation pinned to a profile."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import socketio

from agent_backbone.api.session_updates import SessionFeed
from agent_backbone.config import agent_api_key

FIXTURES = json.loads((Path(__file__).parents[3] / "docs/contracts/profiles-v1.json").read_text())
HEADERS = FIXTURES["request_headers"]
FIELDS = FIXTURES["socket_auth"]


@pytest.mark.parametrize("binding", [HEADERS, *({k: ""} for k in HEADERS)])
async def test_http_binding_refuses_reads_and_mutations(api_client, auth_headers, binding):
    for method, path, body in (
        ("GET", "/api/agents", None),
        ("PUT", "/api/config/backbone.port", {"value": 9999}),
        ("GET", "/api/profile", None),
    ):
        response = await api_client.request(
            method, path, headers={**auth_headers, **binding}, json=body
        )
        assert response.status_code == FIXTURES["http_errors"]["binding_mismatch"]["status"]
        assert response.json() == FIXTURES["http_errors"]["binding_mismatch"]["body"]


async def test_http_authentication_precedes_binding(api_client):
    response = await api_client.get("/api/agents", headers=HEADERS)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_registered_runtime_clients_cannot_downgrade(api_client, api_app, runtime):
    spec = await api_app.state.agent_store.update("ike", runtime=runtime)
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    headers = {"Authorization": f"Bearer {key}"}
    # All shipped runtime clients use the same authenticated provider boundary.
    assert (await api_client.get("/api/reports", headers=headers)).status_code == 200
    response = await api_client.get("/api/agents", headers={**headers, **HEADERS})
    assert response.status_code == 409
    await api_app.state.db.agents.delete(spec.name)
    response = await api_client.get("/api/agents", headers={**headers, **HEADERS})
    assert response.status_code == 403
    assert response.json() == {"detail": "Agent credential registration is retired"}


@pytest.mark.parametrize("namespace", ["/sessions", "/terminal"])
@pytest.mark.parametrize("binding", [FIELDS, *({k: None} for k in FIELDS)])
async def test_socket_binding_rejects_before_subscription(api_app, namespace, binding):
    sio = api_app.state.sio
    ns = sio.namespace_handlers[namespace]
    api_app.state.feed.snapshot = AsyncMock()
    with pytest.raises(socketio.exceptions.ConnectionRefusedError) as raised:
        await ns.on_connect("bound", {}, {"api_key": api_app.state.config.api_key, **binding})
    assert raised.value.error_args == FIXTURES["socket_errors"]["binding_mismatch"]["payload"]
    api_app.state.feed.snapshot.assert_not_awaited()
    assert ns._registrations == {}
    if namespace == "/terminal":
        assert ns._subscriptions == {}


@pytest.mark.parametrize("namespace", ["/sessions", "/terminal"])
async def test_socket_authentication_precedes_binding(api_app, namespace):
    ns = api_app.state.sio.namespace_handlers[namespace]
    assert await ns.on_connect("bad", {}, {"api_key": "wrong", **FIELDS}) is False


async def test_unbound_identity_endpoint_remains_unavailable(api_client, auth_headers):
    assert (await api_client.get("/api/profile", headers=auth_headers)).status_code == 404


@pytest.fixture
async def bound_provider(api_app, tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_backbone.config import sqlite_url
    from agent_backbone.services.database import (
        BackboneDB,
        ProfileIdentity,
        ProfileRegistry,
        ProfileReservation,
        reconcile_profile_provider,
    )
    from agent_backbone.services.terminal import _server, pin_tmux_server

    # Only this test installs a reconciled context. Production activation is
    # deliberately still refused by the unchanged startup guard.
    root = tmp_path / "provider"
    root.mkdir(mode=0o700)
    path = root / "backbone.db"
    async with BackboneDB.connect(sqlite_url(root)) as db:
        await db.agents.upsert(
            "ike",
            dir=str(root),
            runtime="claude",
            model=None,
            repo="",
            tags=[],
            env={},
            description="",
            unattended=False,
        )
        identity = ProfileIdentity("profile-a", await db.signing.audience(), "lifecycle-a1")
        await db.profile_identity.reserve(identity)
        registry = ProfileRegistry.create(tmp_path / "registry" / "registry.db")
        reservation = registry.reserve(ProfileReservation(identity, root, "Work", "baseline-v1"))
        provider = await reconcile_profile_provider(registry, reservation, path, root / "tmux.sock")
        monkeypatch.setattr(_server, "_socket", None)
        monkeypatch.setattr(_server, "_used", False)
        pin_tmux_server(provider.socket_path)
        monkeypatch.setattr(api_app.state, "db", db)
        monkeypatch.setattr(
            api_app.state,
            "config",
            replace(
                api_app.state.config,
                backbone=replace(api_app.state.config.backbone, data_dir=str(root)),
            ),
        )
        monkeypatch.setattr(api_app.state.agent_store, "_db", db)
        api_app.state.profile_provider = provider
        headers = {
            f"X-Backbone-{field.replace('_', '-')}": value
            for field, value in vars(identity).items()
        }
        yield provider, registry, headers


async def test_identity_requires_authentication_even_on_legacy(api_client):
    assert (await api_client.get("/api/profile")).status_code == 401


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_admitted_http_effect_finishes_before_invalidation(
    api_client, api_app, bound_provider, runtime
):
    provider, registry, headers = bound_provider
    spec = await api_app.state.agent_store.update("ike", runtime=runtime)
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    headers = {**headers, "Authorization": f"Bearer {key}"}
    entered, finish = asyncio.Event(), asyncio.Event()
    effects = []

    @api_app.post("/api/fenced-effect")
    async def effect():
        entered.set()
        await finish.wait()
        effects.append("original provider")
        return {"ok": True}

    request = asyncio.create_task(api_client.post("/api/fenced-effect", headers=headers))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        assert registry.show(provider.reservation.identity.profile_id)["state"] == "creating"
        finish.set()
        assert (await asyncio.wait_for(request, 5)).status_code == 200
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    assert (await api_client.post("/api/fenced-effect", headers=headers)).status_code == 409
    assert effects == ["original provider"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_stream_retains_fence_until_finished_or_cancelled(
    api_client, api_app, auth_headers, bound_provider, cancel
):
    from starlette.responses import StreamingResponse

    provider, registry, headers = bound_provider
    entered, finish = asyncio.Event(), asyncio.Event()

    async def chunks():
        yield b"first"
        entered.set()
        await finish.wait()
        yield b"last"

    @api_app.get("/api/fenced-stream")
    async def stream():
        return StreamingResponse(chunks())

    request = asyncio.create_task(
        api_client.get("/api/fenced-stream", headers={**auth_headers, **headers})
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        if cancel:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            finish.set()
            assert (await asyncio.wait_for(request, 5)).content == b"firstlast"
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


async def test_bound_identity_handshake_and_replaced_endpoint(
    api_client, auth_headers, bound_provider
):
    provider, _, headers = bound_provider
    response = await api_client.get("/api/profile", headers={**auth_headers, **headers})
    assert response.status_code == 200
    assert response.json() == provider.identity_response()
    # The same credentials reaching a replacement identity cannot replay the
    # original operation after a successful handshake.
    stale = {**headers, "X-Backbone-lifecycle-id": "previous-generation"}
    response = await api_client.get("/api/profile", headers={**auth_headers, **stale})
    assert response.status_code == 409
    assert response.json() == FIXTURES["http_errors"]["binding_mismatch"]["body"]


async def test_bound_requests_fail_before_content(
    api_client, api_app, auth_headers, bound_provider
):
    provider, registry, headers = bound_provider
    effects = []

    @api_app.post("/binding-test-effect")
    async def effect():
        effects.append("effect")
        return {}

    for binding, code in (
        ({}, "binding_required"),
        ({next(iter(headers)): ""}, "binding_mismatch"),
    ):
        response = await api_client.post(
            "/binding-test-effect", headers={**auth_headers, **binding}
        )
        assert response.json() == FIXTURES["http_errors"][code]["body"]
        assert response.status_code == 409
    assert (await api_client.post("/binding-test-effect", headers=headers)).status_code == 401
    assert not effects
    assert (
        await api_client.post("/binding-test-effect", headers={**auth_headers, **headers})
    ).status_code == 200
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    response = await api_client.post("/binding-test-effect", headers={**auth_headers, **headers})
    assert response.status_code == 409
    assert response.json() == FIXTURES["http_errors"]["binding_mismatch"]["body"]
    assert effects == ["effect"]


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_bound_registered_clients_keep_registration_checks(
    api_client, api_app, bound_provider, runtime
):
    _, _, headers = bound_provider
    spec = await api_app.state.agent_store.update("ike", runtime=runtime)
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    auth = {"Authorization": f"Bearer {key}", **headers}
    assert (await api_client.get("/api/profile", headers=auth)).status_code == 200
    await api_app.state.db.agents.delete(spec.name)
    assert (await api_client.get("/api/profile", headers=auth)).status_code == 403


@pytest.mark.parametrize("namespace", ["/sessions", "/terminal"])
async def test_bound_socket_checks_before_subscription_and_after_invalidation(
    api_app, bound_provider, namespace
):
    provider, registry, _ = bound_provider
    ns = api_app.state.sio.namespace_handlers[namespace]
    api_app.state.feed.snapshot = AsyncMock(return_value=[])
    ns.emit = AsyncMock()
    auth = {"api_key": api_app.state.config.api_key}
    with pytest.raises(socketio.exceptions.ConnectionRefusedError) as raised:
        await ns.on_connect("unbound", {}, auth)
    assert raised.value.error_args == FIXTURES["socket_errors"]["binding_required"]["payload"]
    api_app.state.feed.snapshot.assert_not_awaited()
    auth.update(vars(provider.reservation.identity))
    assert await ns.on_connect("bound", {}, auth)
    assert await ns.registration_current("bound")
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    ns.disconnect = AsyncMock()
    assert not await ns.registration_current("bound")
    ns.disconnect.assert_awaited_once_with("bound")
    await ns.on_disconnect("bound")
    assert not ns._profiles


async def test_wrong_terminal_pin_and_duplicate_headers_fail_closed(
    api_client, auth_headers, bound_provider, monkeypatch
):
    _, _, headers = bound_provider
    duplicate = list({**auth_headers, **headers}.items()) + [(next(iter(headers)), "other")]
    assert (await api_client.get("/api/profile", headers=duplicate)).status_code == 409
    monkeypatch.setattr("agent_backbone.api.profile_binding.tmux_server_socket", lambda: None)
    assert (
        await api_client.get("/api/profile", headers={**auth_headers, **headers})
    ).status_code == 409


@pytest.mark.parametrize("namespace", ["/sessions", "/terminal"])
async def test_invalidation_disconnects_operator_before_broadcast(
    api_app, bound_provider, namespace
):
    provider, registry, _ = bound_provider
    sio = api_app.state.sio
    sio._send_packet = AsyncMock()
    sio._send_eio_packet = AsyncMock()
    api_app.state.feed.snapshot = AsyncMock(return_value=[])
    sid = await sio.manager.connect("engine", namespace)
    ns = sio.namespace_handlers[namespace]
    assert await ns.on_connect(
        sid,
        {},
        {
            "api_key": api_app.state.config.api_key,
            **vars(provider.reservation.identity),
        },
    )
    sio._send_packet.reset_mock()
    sio._send_eio_packet.reset_mock()
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    await sio.emit("sentinel", {"text": "must not be sent"}, namespace=namespace)
    assert not sio.manager.is_connected(sid, namespace)
    assert sid not in ns._profiles
    assert all(call.args[1].packet_type == 1 for call in sio._send_packet.await_args_list)
    sio._send_eio_packet.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
async def test_detached_history_cleanup_keeps_profile_fenced(
    api_app, bound_provider, monkeypatch, cancel
):
    from agent_backbone.api import session_updates

    provider, registry, _ = bound_provider
    feed = SessionFeed(lambda: api_app.state.config, db=api_app.state.db, profile_provider=provider)
    entered, cancelling, release = (asyncio.Event() for _ in range(3))

    async def stalled_write(states):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()

    monkeypatch.setattr(session_updates, "build_session_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(session_updates, "HISTORY_WRITE_TIMEOUT_SECONDS", 10 if cancel else 0.1)
    monkeypatch.setattr(api_app.state.db.state_history, "observe", stalled_write)
    snapshot = asyncio.create_task(feed.snapshot())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if cancel:
            snapshot.cancel()
            with pytest.raises(asyncio.CancelledError):
                await snapshot
        else:
            assert await asyncio.wait_for(snapshot, 3) == []
        await asyncio.wait_for(cancelling.wait(), 3)
        assert not feed._history_task.done()
        with pytest.raises(ValueError, match="busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    finally:
        release.set()
        await asyncio.gather(snapshot, return_exceptions=True)
        await feed.close()
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


@pytest.mark.parametrize("change", ["invalidate", "database", "config", "terminal"])
async def test_history_refuses_retired_or_mismatched_context(
    api_app, bound_provider, monkeypatch, change
):
    from dataclasses import replace

    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.terminal import _server

    provider, registry, _ = bound_provider
    feed = SessionFeed(lambda: api_app.state.config, db=api_app.state.db, profile_provider=provider)
    observe = AsyncMock()
    monkeypatch.setattr(api_app.state.db.state_history, "observe", observe)
    if change == "invalidate":
        registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    elif change == "database":
        feed._db = BackboneDB("sqlite+aiosqlite:///:memory:")
        monkeypatch.setattr(feed._db.state_history, "observe", observe)
    elif change == "config":
        monkeypatch.setattr(
            api_app.state,
            "config",
            replace(
                api_app.state.config,
                backbone=replace(api_app.state.config.backbone, data_dir="/other-provider"),
            ),
        )
    else:
        monkeypatch.setattr(_server, "_socket", "/other-provider/tmux.sock")
    await feed._record_history([("ike", "idle", None)])
    observe.assert_not_awaited()


async def test_context_cannot_attest_another_serving_database(
    api_client, api_app, auth_headers, bound_provider, monkeypatch
):
    from agent_backbone.services.database import BackboneDB

    _, _, headers = bound_provider
    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as other:
        monkeypatch.setattr(api_app.state, "db", other)
        response = await api_client.get("/api/profile", headers={**auth_headers, **headers})
        assert response.status_code == 409
        assert response.json() == FIXTURES["http_errors"]["binding_mismatch"]["body"]


async def test_pending_binding_cannot_receive_broadcast(api_app, bound_provider, monkeypatch):
    import asyncio

    from agent_backbone.api import socketio_server

    provider, registry, _ = bound_provider
    entered, release = asyncio.Event(), asyncio.Event()
    check = socketio_server.binding_error

    async def delayed_check(*args):
        entered.set()
        await release.wait()
        return await check(*args)

    monkeypatch.setattr(socketio_server, "binding_error", delayed_check)
    sio = api_app.state.sio
    sio._send_packet = AsyncMock()
    sio._send_eio_packet = AsyncMock()
    sid = await sio.manager.connect("engine", "/sessions")
    ns = sio.namespace_handlers["/sessions"]
    connecting = asyncio.create_task(
        ns.on_connect(
            sid,
            {},
            {
                "api_key": api_app.state.config.api_key,
                **vars(provider.reservation.identity),
                "lifecycle_id": "stale",
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await sio.emit("sessions:update", {"text": "must not be sent"}, namespace="/sessions")
        sio._send_packet.assert_not_awaited()
        sio._send_eio_packet.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(socketio.exceptions.ConnectionRefusedError):
            await connecting
        await sio.manager.disconnect(sid, "/sessions")


async def test_broadcast_cannot_expand_recipients_during_validation(
    api_app, bound_provider, monkeypatch
):
    provider, _, _ = bound_provider
    sio = api_app.state.sio
    sio._send_packet = AsyncMock()
    sio._send_eio_packet = AsyncMock()
    api_app.state.feed.snapshot = AsyncMock(return_value=[])
    sid = await sio.manager.connect("engine-admitted", "/sessions")
    ns = sio.namespace_handlers["/sessions"]
    assert await ns.on_connect(
        sid,
        {},
        {
            "api_key": api_app.state.config.api_key,
            **vars(provider.reservation.identity),
        },
    )
    sio._send_eio_packet.reset_mock()
    check = ns.registration_current

    async def admit_during_check(current, **kwargs):
        # Socket.IO joins the room before invoking the pending handshake.
        await sio.manager.connect("engine-pending", "/sessions")
        return await check(current, **kwargs)

    monkeypatch.setattr(ns, "registration_current", admit_during_check)
    await sio.emit("sessions:update", {"text": "admitted only"}, namespace="/sessions")
    assert [call.args[0] for call in sio._send_eio_packet.await_args_list] == ["engine-admitted"]


@pytest.mark.parametrize("prefix", ["", "/bb"])
async def test_unbound_probes_and_webhooks_keep_route_authentication(
    api_client, api_app, auth_headers, bound_provider, prefix
):
    import hashlib
    import hmac

    provider, registry, headers = bound_provider
    api_app.root_path = prefix
    api_client.base_url = f"http://testserver{prefix}/"
    body = b"{}"
    signature = (
        "sha256="
        + hmac.new(api_app.state.config.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    webhook = {"X-GitHub-Event": "ping", "X-Hub-Signature-256": signature}
    assert (await api_client.get("/health")).status_code == 200
    response = await api_client.post("/webhooks/github", content=body, headers=webhook)
    assert response.status_code == 200
    assert response.text == "pong"
    response = await api_client.post("/webhooks/github", content=body)
    assert response.status_code == 403
    assert response.text == "Invalid signature"
    for method, path in (("GET", "/health"), ("POST", "/webhooks/github")):
        for binding in (headers, {next(iter(headers)): ""}):
            response = await api_client.request(method, path, headers=binding)
            assert response.status_code == 401
        response = await api_client.request(
            method, path, headers={**auth_headers, next(iter(headers)): ""}
        )
        assert response.status_code == 409
    # Only the exact method/path pair is exempt from the provider gate.
    assert (await api_client.post("/health")).status_code == 401
    assert (await api_client.get("/webhooks/github")).status_code == 401
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    for method, path in (("GET", "/health"), ("POST", "/webhooks/github")):
        response = await api_client.request(method, path, content=body, headers=webhook)
        assert response.status_code == 409
        assert response.json() == FIXTURES["http_errors"]["binding_mismatch"]["body"]


@pytest.mark.parametrize("namespace", ["/sessions", "/terminal"])
async def test_broadcast_reconciles_once_and_keeps_recipient_checks(
    api_app, bound_provider, monkeypatch, namespace
):
    from dataclasses import replace

    from agent_backbone.api import socketio_server

    provider, registry, _ = bound_provider
    sio = api_app.state.sio
    sio._send_packet = AsyncMock()
    sio._send_eio_packet = AsyncMock()
    api_app.state.feed.snapshot = AsyncMock(return_value=[])
    ns = sio.namespace_handlers[namespace]
    agent = await api_app.state.agent_store.update("ike", runtime="claude")
    sids = {}
    for name in ("operator", "retired", "other-context"):
        sid = sids[name] = await sio.manager.connect(name, namespace)
        key = api_app.state.config.api_key
        if name == "retired":
            key = agent_api_key(key, agent.registration_id)
        assert await ns.on_connect(sid, {}, {"api_key": key, **vars(provider.reservation.identity)})
    await api_app.state.db.agents.delete("ike")
    ns._profiles[sids["other-context"]] = replace(provider)
    check = AsyncMock(wraps=socketio_server.provider_current)
    monkeypatch.setattr(socketio_server, "provider_current", check)
    sio._send_eio_packet.reset_mock()
    await sio.emit("sentinel", {}, namespace=namespace, skip_sid=list(sids.values()))
    check.assert_not_awaited()
    await sio.emit("sentinel", {}, namespace=namespace, room="empty-room")
    check.assert_not_awaited()
    await sio.emit("sentinel", {}, namespace=namespace)
    check.assert_awaited_once_with(provider, api_app)
    assert [call.args[0] for call in sio._send_eio_packet.await_args_list] == ["operator"]
    assert not sio.manager.is_connected(sids["retired"], namespace)
    assert not sio.manager.is_connected(sids["other-context"], namespace)
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    sio._send_eio_packet.reset_mock()
    await sio.emit("sentinel", {}, namespace=namespace)
    assert check.await_count == 1  # Failed admission refuses before reconciliation.
    assert not sio.manager.is_connected(sids["operator"], namespace)
    sio._send_eio_packet.assert_not_awaited()


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_socket_event_holds_fence_through_handler(api_app, bound_provider, runtime, cancel):
    provider, registry, _ = bound_provider
    await api_app.state.agent_store.update("ike", runtime=runtime)
    sio = api_app.state.sio
    sio._send_packet = AsyncMock()
    ns = sio.namespace_handlers["/terminal"]
    sid = await sio.manager.connect("engine", ns.namespace)
    assert await ns.on_connect(
        sid, {}, {"api_key": api_app.state.config.api_key, **vars(provider.reservation.identity)}
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    cleaning, cleaned = asyncio.Event(), asyncio.Event()
    effects = []

    async def pause(sid, data):
        entered.set()
        try:
            await finish.wait()
            effects.append(data)
        finally:
            cleaning.set()
            await cleaned.wait()

    ns.on_pause = pause
    event = asyncio.create_task(ns.trigger_event("pause", sid, {"session": "ike"}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        if cancel:
            event.cancel()
            await asyncio.wait_for(cleaning.wait(), 5)
            with pytest.raises(ValueError, match="fence is busy"):
                registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
            cleaned.set()
            # Socket.IO preserves its existing cancellation-to-None behavior.
            assert await asyncio.wait_for(event, 5) is None
        else:
            finish.set()
            cleaned.set()
            await asyncio.wait_for(event, 5)
    finally:
        finish.set()
        cleaned.set()
        await asyncio.gather(event, return_exceptions=True)
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    await ns.trigger_event("pause", sid, {"session": "retired"})
    assert effects == ([] if cancel else [{"session": "ike"}])
    assert not sio.manager.is_connected(sid, ns.namespace)


@pytest.mark.parametrize("namespace,callback", [("/sessions", None), ("/terminal", lambda: None)])
@pytest.mark.parametrize("cancel", [False, True])
async def test_socket_emission_holds_fence_until_packet_tasks_finish(
    api_app, bound_provider, namespace, callback, cancel
):
    provider, registry, _ = bound_provider
    sio = api_app.state.sio
    sio._send_packet = AsyncMock()
    sio._send_eio_packet = AsyncMock()
    api_app.state.feed.snapshot = AsyncMock(return_value=[])
    ns = sio.namespace_handlers[namespace]
    sid = await sio.manager.connect("engine", namespace)
    assert await ns.on_connect(
        sid, {}, {"api_key": api_app.state.config.api_key, **vars(provider.reservation.identity)}
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    sent = []

    async def send(*args):
        entered.set()
        await finish.wait()
        sent.append(args[0])

    sio._send_packet = AsyncMock(side_effect=send)
    sio._send_eio_packet = AsyncMock(side_effect=send)
    emission = asyncio.create_task(sio.emit("sentinel", {}, namespace=namespace, callback=callback))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        if cancel:
            emission.cancel()
            emission.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(emission, 5)
            with pytest.raises(ValueError, match="fence is busy"):
                registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        finish.set()
        if not cancel:
            await asyncio.wait_for(emission, 5)
        await asyncio.wait_for(asyncio.gather(*sio._profile_emissions), 5)
        assert sent == ["engine"]
        assert not sio._profile_emissions
    finally:
        finish.set()
        await asyncio.gather(emission, *sio._profile_emissions, return_exceptions=True)
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


async def test_terminal_packet_timeout_can_finish_disconnect_cleanup(
    api_app, bound_provider, monkeypatch
):
    import time
    from types import SimpleNamespace

    from engineio.async_socket import AsyncSocket

    provider, registry, _ = bound_provider
    sio = api_app.state.sio
    ns = sio.namespace_handlers["/terminal"]
    sid = await sio.manager.connect("engine", ns.namespace)
    assert await ns.on_connect(
        sid, {}, {"api_key": api_app.state.config.api_key, **vars(provider.reservation.identity)}
    )
    socket = AsyncSocket(sio.eio, "engine")
    socket.last_ping = time.time() - sio.eio.ping_timeout - 1
    sio.eio.sockets["engine"] = socket
    manager = SimpleNamespace(remove=AsyncMock())
    monkeypatch.setattr("agent_backbone.api.socketio_server.get_pty_manager", lambda: manager)
    packet_tasks = []
    emission_tasks = set()
    sent = asyncio.Event()
    original_send = sio._send_eio_packet

    async def send(*args):
        packet_tasks.append(asyncio.current_task())
        emission_tasks.update(
            task
            for task in asyncio.all_tasks()
            if task.get_coro().__qualname__ == "AsyncServer.emit"
        )
        try:
            return await original_send(*args)
        finally:
            sent.set()

    monkeypatch.setattr(sio, "_send_eio_packet", send)
    queue = asyncio.Queue()
    manager.create = AsyncMock(return_value=SimpleNamespace(output_queue=queue))
    await ns._attach_subscription_client(sid, "ike", 80, 24)
    forwarding = ns._subscriptions[sid]["ike"]
    queue.put_nowait("sentinel")
    try:
        await asyncio.wait_for(asyncio.shield(forwarding), 2)
        await asyncio.wait_for(sent.wait(), 2)
        assert socket.closed
        manager.remove.assert_awaited_once_with(sid, "ike")
        assert sid not in ns._subscriptions
        assert not sio.manager.is_connected(sid, ns.namespace)
    finally:
        for task in emission_tasks:
            task.cancel()
        for task in packet_tasks:
            task.cancel()
        forwarding.cancel()
        await asyncio.gather(forwarding, *packet_tasks, *emission_tasks, return_exceptions=True)
        await asyncio.gather(*getattr(sio, "_profile_emissions", ()), return_exceptions=True)
        await asyncio.gather(*tuple(ns._background), return_exceptions=True)
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
@pytest.mark.parametrize("stage", ["registration", "snapshot"])
async def test_socket_admission_holds_original_fence(
    api_app, bound_provider, monkeypatch, runtime, stage
):
    provider, registry, _ = bound_provider
    agent = await api_app.state.agent_store.update("ike", runtime=runtime)
    ns = api_app.state.sio.namespace_handlers["/sessions"]
    ns.emit = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()
    read_registration = api_app.state.db.agents.registration_name

    async def registration(registration_id):
        entered.set()
        await release.wait()
        return await read_registration(registration_id)

    async def snapshot():
        entered.set()
        await release.wait()
        return []

    api_app.state.feed.snapshot = AsyncMock(return_value=[])
    if stage == "registration":
        monkeypatch.setattr(api_app.state.db.agents, "registration_name", registration)
    else:
        api_app.state.feed.snapshot = snapshot
    connecting = asyncio.create_task(
        ns.trigger_event(
            "connect",
            "pending",
            {},
            {
                "api_key": agent_api_key(api_app.state.config.api_key, agent.registration_id),
                **vars(provider.reservation.identity),
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        release.set()
        assert await asyncio.wait_for(connecting, 3) is True
        assert not ns._connecting
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)
        await ns.on_disconnect("pending")
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


@pytest.mark.parametrize("change", ["provider", "config", "database", "disconnect", "cancel"])
async def test_pending_connection_cannot_recapture_or_outlive_admission(
    api_app, bound_provider, monkeypatch, change
):
    from dataclasses import replace

    provider, registry, _ = bound_provider
    agent = await api_app.state.agent_store.update("ike", runtime="codex")
    ns = api_app.state.sio.namespace_handlers["/sessions"]
    entered, release = asyncio.Event(), asyncio.Event()
    read_registration = api_app.state.db.agents.registration_name

    async def registration(registration_id):
        entered.set()
        await release.wait()
        return await read_registration(registration_id)

    monkeypatch.setattr(api_app.state.db.agents, "registration_name", registration)
    api_app.state.feed.snapshot = AsyncMock(return_value=[])
    ns.emit = AsyncMock()
    connecting = asyncio.create_task(
        ns.trigger_event(
            "connect",
            "pending",
            {},
            {
                "api_key": agent_api_key(api_app.state.config.api_key, agent.registration_id),
                **vars(provider.reservation.identity),
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "provider":
            monkeypatch.setattr(api_app.state, "profile_provider", replace(provider))
        elif change == "config":
            monkeypatch.setattr(api_app.state, "config", replace(api_app.state.config))
        elif change == "database":
            monkeypatch.setattr(api_app.state, "db", object())
        elif change == "disconnect":
            await ns.on_disconnect("pending")
        else:
            connecting.cancel()
        release.set()
        assert await asyncio.wait_for(connecting, 3) is False
        assert not ns._profiles
        assert not ns._registrations
        assert not ns._connecting
        api_app.state.feed.snapshot.assert_not_awaited()
        ns.emit.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


@pytest.mark.parametrize("change", ["disconnect", "cancel", "feed"])
async def test_initial_snapshot_drops_failed_admission(
    api_app, bound_provider, monkeypatch, change
):
    provider, registry, _ = bound_provider
    ns = api_app.state.sio.namespace_handlers["/sessions"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def snapshot():
        entered.set()
        await release.wait()
        return []

    api_app.state.feed.snapshot = snapshot
    ns.emit = AsyncMock()
    connecting = asyncio.create_task(
        ns.trigger_event(
            "connect",
            "pending",
            {},
            {"api_key": api_app.state.config.api_key, **vars(provider.reservation.identity)},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "disconnect":
            await ns.on_disconnect("pending")
        elif change == "feed":
            monkeypatch.setattr(api_app.state, "feed", object())
        else:
            connecting.cancel()
        release.set()
        assert await asyncio.wait_for(connecting, 3) is False
        assert not ns._profiles
        assert not ns._registrations
        assert not ns._connecting
        ns.emit.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


async def test_cancelled_handshake_refuses_and_removes_socket_membership(api_app, bound_provider):
    provider, _, _ = bound_provider
    sio = api_app.state.sio
    ns = sio.namespace_handlers["/sessions"]
    entered = asyncio.Event()

    async def snapshot():
        entered.set()
        await asyncio.Event().wait()

    api_app.state.feed.snapshot = snapshot
    sio.environ["engine"] = {}
    sio._send_packet = AsyncMock()
    connecting = asyncio.create_task(
        sio._handle_connect(
            "engine",
            ns.namespace,
            {"api_key": api_app.state.config.api_key, **vars(provider.reservation.identity)},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        connecting.cancel()
        await asyncio.wait_for(connecting, 3)
        assert not list(sio.manager.get_participants(ns.namespace, None))
        assert not ns._profiles and not ns._registrations and not ns._connecting
        assert [call.args[1].packet_type for call in sio._send_packet.await_args_list] == [4]
    finally:
        connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)


async def test_failed_admission_fence_exit_retires_captured_state(
    api_app, bound_provider, monkeypatch
):
    from contextlib import asynccontextmanager

    provider, registry, _ = bound_provider
    operation = type(provider).operation

    @asynccontextmanager
    async def failed_exit(self):
        async with operation(self):
            yield
        raise OSError("fence exit failed")

    monkeypatch.setattr(type(provider), "operation", failed_exit)
    agent = await api_app.state.agent_store.update("ike", runtime="codex")
    ns = api_app.state.sio.namespace_handlers["/sessions"]
    api_app.state.feed.snapshot = AsyncMock(return_value=[])
    ns.emit = AsyncMock()
    sio = api_app.state.sio
    sio.environ["engine"] = {}
    sio._send_packet = AsyncMock()
    with pytest.raises(OSError, match="fence exit failed"):
        await sio._handle_connect(
            "engine",
            ns.namespace,
            {
                "api_key": agent_api_key(api_app.state.config.api_key, agent.registration_id),
                **vars(provider.reservation.identity),
            },
        )
    assert not ns._profiles and not ns._registrations and not ns._connecting
    assert not list(sio.manager.get_participants(ns.namespace, None))
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
@pytest.mark.parametrize(
    "ending",
    [
        "disconnect",
        "cancel",
        "eof",
        "forwarder_cancel",
        "failed_cleanup",
        "failed_disconnect",
        "shutdown",
    ],
)
async def test_attached_terminal_owns_fence_until_resource_cleanup(
    api_app, bound_provider, monkeypatch, runtime, ending
):
    from types import SimpleNamespace

    provider, registry, _ = bound_provider
    spec = await api_app.state.agent_store.update("ike", runtime=runtime)
    sio = api_app.state.sio
    ns = sio.namespace_handlers["/terminal"]
    sid = await sio.manager.connect("engine", ns.namespace)
    assert await ns.on_connect(
        sid,
        {},
        {
            "api_key": agent_api_key(api_app.state.config.api_key, spec.registration_id),
            **vars(provider.reservation.identity),
        },
    )
    ns.emit = AsyncMock()
    creating, created, removing, removed = (asyncio.Event() for _ in range(4))
    queue = asyncio.Queue()

    async def create(*args):
        creating.set()
        await created.wait()
        return SimpleNamespace(output_queue=queue)

    fail = ending in {"failed_cleanup", "failed_disconnect"}

    async def remove(*args):
        removing.set()
        await removed.wait()
        if fail:
            raise OSError("process is still alive")

    manager = SimpleNamespace(
        create=AsyncMock(side_effect=create), remove=AsyncMock(side_effect=remove)
    )
    monkeypatch.setattr("agent_backbone.api.socketio_server.get_pty_manager", lambda: manager)
    attach = asyncio.create_task(ns._attach_subscription_client(sid, "ike", 80, 24))
    disconnect = None
    try:
        await asyncio.wait_for(creating.wait(), 2)
        replacement = SimpleNamespace(create=AsyncMock(), remove=AsyncMock())
        monkeypatch.setattr(
            "agent_backbone.api.socketio_server.get_pty_manager", lambda: replacement
        )
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        if ending in {"disconnect", "failed_disconnect"}:
            disconnect = asyncio.create_task(ns.on_disconnect(sid))
            await asyncio.sleep(0)
        elif ending == "shutdown":
            disconnect = asyncio.create_task(sio.shutdown())
            await asyncio.sleep(0)
        elif ending == "cancel":
            attach.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attach
        created.set()
        if ending not in {"cancel", "disconnect", "failed_disconnect", "shutdown"}:
            await attach
            with pytest.raises(ValueError, match="fence is busy"):
                registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
            # A forwarder ending naturally must trigger process/worker cleanup.
            if ending == "forwarder_cancel":
                ns._subscriptions[sid]["ike"].cancel()
            else:
                queue.put_nowait(None)
        await asyncio.wait_for(removing.wait(), 2)
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        removed.set()
        await asyncio.gather(attach, *([disconnect] if disconnect else []), return_exceptions=True)
        while ns._resource_tasks or ns._background:
            await asyncio.gather(
                *(t for pending in tuple(ns._resource_tasks.values()) for t in tuple(pending)),
                *tuple(ns._background),
                return_exceptions=True,
            )
        if fail:
            with pytest.raises(ValueError, match="fence is busy"):
                registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
            assert ns._resources
            fail = False
            await ns._cleanup_session(sid, "ike")
        assert not ns._resources
        manager.remove.assert_awaited()
        replacement.remove.assert_not_awaited()
        assert all(task is None for task in ns._subscriptions.get(sid, {}).values())
        registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    finally:
        fail = False
        created.set()
        removed.set()
        await asyncio.gather(attach, *([disconnect] if disconnect else []), return_exceptions=True)
        await ns.on_disconnect(sid)
        while ns._resource_tasks or ns._background:
            await asyncio.gather(
                *(t for pending in tuple(ns._resource_tasks.values()) for t in tuple(pending)),
                *tuple(ns._background),
                return_exceptions=True,
            )


async def test_terminal_join_cannot_attach_after_disconnect_during_validation(
    api_app, bound_provider, monkeypatch
):
    provider, registry, _ = bound_provider
    sio = api_app.state.sio
    ns = sio.namespace_handlers["/terminal"]
    sid = await sio.manager.connect("engine", ns.namespace)
    assert await ns.on_connect(
        sid, {}, {"api_key": api_app.state.config.api_key, **vars(provider.reservation.identity)}
    )
    entered, finish = asyncio.Event(), asyncio.Event()

    async def exists(*args):
        entered.set()
        await finish.wait()
        return True

    from types import SimpleNamespace

    manager = SimpleNamespace(create=AsyncMock(), remove=AsyncMock())
    monkeypatch.setattr("agent_backbone.api.socketio_server.get_pty_manager", lambda: manager)
    monkeypatch.setattr("agent_backbone.api.socketio_server.session_exists", exists)
    join = asyncio.create_task(
        ns.trigger_event("join", sid, {"session": "ike", "cols": 80, "rows": 24})
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await sio.disconnect(sid, namespace=ns.namespace)
    finally:
        finish.set()
        await join
    manager.create.assert_not_called()
    assert not ns._resources
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)


async def test_validation_notice_retains_original_provider_until_shutdown_drains(
    api_app, bound_provider, monkeypatch
):
    from dataclasses import replace

    from agent_backbone.api import validation

    provider, registry, _ = bound_provider
    original = api_app.state.config
    entered, release = asyncio.Event(), asyncio.Event()

    async def delay(_seconds):
        entered.set()
        await release.wait()

    notify = AsyncMock()
    monkeypatch.setattr(validation, "_sleep", delay)
    monkeypatch.setattr(validation, "notify_humans", notify)
    api_app.state.validations.failed(
        original,
        "worker",
        "confirmation",
        "unknown_confirmation",
        0,
        provider=provider,
        db=api_app.state.db,
    )
    await asyncio.wait_for(entered.wait(), 5)
    # Changing app selection cannot redirect the accepted notice's configuration.
    api_app.state.config = replace(original, api_key="other-provider")
    api_app.state.profile_provider = None
    shutdown = asyncio.create_task(api_app.state.api_tasks.stop())
    try:
        await asyncio.sleep(0)
        assert not shutdown.done()
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        api_app.state.validations.failed(
            original,
            "later",
            "confirmation",
            "unknown_confirmation",
            1,
            provider=provider,
            db=api_app.state.db,
        )
        release.set()
        await asyncio.wait_for(shutdown, 5)
        notify.assert_awaited_once()
        assert notify.call_args.args[0] is original
        registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    finally:
        release.set()
        await api_app.state.api_tasks.stop()
        await asyncio.gather(shutdown, return_exceptions=True)


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_setup_workers_retain_provider_after_client_and_shutdown_cancellation(
    api_app, api_client, auth_headers, bound_provider, monkeypatch, runtime
):
    import threading
    from types import SimpleNamespace

    from agent_backbone.api.routes import agents

    provider, registry, headers = bound_provider
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def probe():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return {"compatible": True}

    monkeypatch.setattr(
        agents,
        "RUNTIMES",
        {
            runtime: SimpleNamespace(
                id=runtime,
                display_name=runtime,
                models=[],
                available=lambda: True,
                setup_compatibility=probe,
            )
        },
    )
    request = asyncio.create_task(
        api_client.get("/api/runtimes?check_setup=true", headers={**auth_headers, **headers})
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        batch = api_app.state.runtime_setup_checks
        for _ in range(2):
            shutdown = asyncio.create_task(api_app.state.api_tasks.stop())
            await asyncio.sleep(0)
            shutdown.cancel()
            await asyncio.gather(shutdown, return_exceptions=True)
            assert not batch.done()
            with pytest.raises(ValueError, match="fence is busy"):
                registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        # Event-loop teardown can cancel the owned task itself as well.
        for _ in range(2):
            batch.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not batch.done()
            with pytest.raises(ValueError, match="fence is busy"):
                registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
        release.set()
        await asyncio.wait_for(api_app.state.api_tasks.stop(), 5)
        assert batch.cancelled()
        response = await api_client.get(
            "/api/runtimes?check_setup=true", headers={**auth_headers, **headers}
        )
        assert response.status_code == 503
        registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await api_app.state.api_tasks.stop()


async def test_detached_api_task_refuses_retired_or_mismatched_provider(api_app, bound_provider):
    from dataclasses import replace

    provider, registry, _ = bound_provider
    run = AsyncMock()
    original = api_app.state.config
    task = api_app.state.api_tasks.create(
        run,
        config=replace(original, backbone=replace(original.backbone, data_dir="/other")),
        db=api_app.state.db,
        provider=provider,
    )
    with pytest.raises(ValueError, match="context does not match"):
        await task
    task = api_app.state.api_tasks.create(
        run,
        config=original,
        db=api_app.state.db,
        provider=provider,
    )
    # Scheduling alone does not authorize execution. Retirement before the
    # task acquires its guard must refuse work without effects.
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    with pytest.raises(ValueError):
        await task
    run.assert_not_awaited()
    await api_app.state.api_tasks.stop()


async def test_http_validation_notice_captures_request_provider(
    api_app, api_client, auth_headers, bound_provider, monkeypatch
):
    from agent_backbone.api import validation
    from agent_backbone.services.terminal import CallerUnknown

    provider, registry, headers = bound_provider
    entered, release = asyncio.Event(), asyncio.Event()

    async def delay(_seconds):
        entered.set()
        await release.wait()

    monkeypatch.setattr(validation, "_sleep", delay)
    monkeypatch.setattr(validation, "caller_agent", AsyncMock(side_effect=CallerUnknown("test")))
    notify = AsyncMock()
    monkeypatch.setattr(validation, "notify_humans", notify)
    try:
        response = await api_client.post(
            "/api/messages/validate",
            headers={**auth_headers, **headers},
            json={"confirmation_id": "12345678-1234-4234-8234-123456789abc"},
        )
        assert response.status_code == 403
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
    finally:
        release.set()
        await api_app.state.api_tasks.stop()
    notify.assert_awaited_once()
    assert notify.call_args.args[0] is api_app.state.config
    registry.invalidate(provider.reservation.identity, provider.reservation.data_dir)
