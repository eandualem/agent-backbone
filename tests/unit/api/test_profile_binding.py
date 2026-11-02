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
