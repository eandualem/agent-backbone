"""Only the installation operator can inspect identity or execute setup."""

from unittest.mock import AsyncMock

import pytest

from agent_backbone.api.routes import onboarding
from agent_backbone.config import agent_api_key
from agent_backbone.models import SETUP_BODY_BYTES, SETUP_CANCEL_BODY_BYTES


async def test_identity_is_the_configured_installation(api_client, config, auth_headers):
    response = await api_client.get("/api/setup/identity", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"schema_version": 1, "data_dir": str(config.data_dir.resolve())}


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/setup/identity", "GET"),
        ("/api/setup/first-agent", "POST"),
        ("/api/setup/first-agent/cancel", "POST"),
    ],
)
async def test_agent_credential_cannot_run_setup(api_client, config, path, method, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(onboarding, "first_agent", execute)
    monkeypatch.setattr(onboarding, "cancel_first_agent", execute)
    response = await api_client.request(
        method, path, json={}, headers={"Authorization": "Bearer " + agent_api_key(config.api_key)}
    )
    assert response.status_code == 403
    execute.assert_not_awaited()


@pytest.mark.parametrize("path", ["/api/setup/first-agent", "/api/setup/first-agent/cancel"])
async def test_setup_body_is_bounded_and_invalid_values_are_not_echoed(
    api_client, auth_headers, path
):
    response = await api_client.post(
        path,
        headers=auth_headers,
        content=b"x"
        * ((SETUP_CANCEL_BODY_BYTES if path.endswith("/cancel") else SETUP_BODY_BYTES) + 1),
    )
    assert response.status_code == 413
    response = await api_client.post(
        path, headers=auth_headers, json={"secret": "must-not-echo-this-value"}
    )
    assert response.status_code == 422
    assert "must-not-echo-this-value" not in response.text


async def test_compatibility_probe_requires_operator_and_runs_off_event_loop(
    api_client, config, auth_headers, monkeypatch
):
    import threading

    from agent_backbone.services.runtimes import RUNTIMES

    event_thread = threading.get_ident()
    calls = []

    def probe(self):
        assert threading.get_ident() != event_thread
        calls.append(self.id)
        return {
            "compatible": True,
            "reason": "compatible",
            "message": "Ready.",
            "path": "/selected/tool",
            "version": "2.2.2",
            "version_error": None,
        }

    for rt in RUNTIMES.values():
        monkeypatch.setattr(type(rt), "setup_compatibility", probe)
    response = await api_client.get(
        "/api/runtimes?check_setup=true",
        headers={"Authorization": "Bearer " + agent_api_key(config.api_key)},
    )
    assert response.status_code == 403 and calls == []
    response = await api_client.get("/api/runtimes", headers=auth_headers)
    assert response.status_code == 200 and calls == []
    response = await api_client.get("/api/runtimes?check_setup=true", headers=auth_headers)
    assert response.status_code == 200 and set(calls) == set(RUNTIMES)
    assert all(row["setup_compatibility"]["path"] == "/selected/tool" for row in response.json())


async def test_cancellation_accepts_largest_original_request(
    api_client, config, auth_headers, monkeypatch
):
    import json

    from agent_backbone.models import SetupFirstAgent, setup_result

    body = {
        "schema_version": 1,
        "request_id": "boundary",
        "action": "first_agent",
        "data_dir": str(config.data_dir.resolve()),
        "hooks": ["codex"],
        "start_greeting": False,
        "agent": {
            "name": "guide",
            "directory": "/workspace/guide",
            "create_directory": True,
            "runtime": "codex",
            "model": None,
            "brief": {"text": "x"},
        },
    }

    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    needed = SETUP_BODY_BYTES - len(encode(body))
    body["agent"]["brief"]["text"] += "界" * (needed // 3) + "x" * (needed % 3)
    SetupFirstAgent.model_validate(body)
    assert len(encode(body)) == SETUP_BODY_BYTES
    body["action"] = "cancel_first_agent"
    assert len(encode(body)) == SETUP_CANCEL_BODY_BYTES
    execute = AsyncMock(
        return_value=setup_result(
            "cancel_first_agent",
            "request_cancelled",
            "Cancelled.",
            status="completed",
            retry="new_request",
        )
    )
    monkeypatch.setattr(onboarding, "cancel_first_agent", execute)
    response = await api_client.post(
        "/api/setup/first-agent/cancel",
        content=encode(body),
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    execute.assert_awaited_once()
