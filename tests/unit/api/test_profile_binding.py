"""Legacy providers must not silently accept an operation pinned to a profile."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import socketio

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
