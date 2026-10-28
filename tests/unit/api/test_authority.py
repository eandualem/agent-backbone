"""Operator authority stays separate from collaboration credentials.

This API boundary applies equally to claude, codex, opencode, gemini,
deepcode, aider and shell; required-runtime launch exports are tested separately.
"""

import asyncio
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_backbone.api.app import create_app
from agent_backbone.api.auth import credential_role
from agent_backbone.config import agent_api_key, load_secrets
from agent_backbone.services.agents import lifecycle_lock
from agent_backbone.services.agents.launch import StartResult


def test_derived_key_cannot_authenticate_as_operator(config, tmp_path, monkeypatch):
    operator = "operator-secret"
    agent = agent_api_key(operator)
    assert credential_role(agent, operator) == "agent"
    assert credential_role(operator, operator) == "operator"
    assert credential_role(agent, "rotated-secret") is None
    (tmp_path / ".env").write_text("BACKBONE_API_KEY=operator-secret\nGITHUB_TOKEN=private\n")
    monkeypatch.setenv("BACKBONE_API_KEY", agent)
    secrets = load_secrets(tmp_path)
    assert secrets["BACKBONE_API_KEY"] == agent
    assert secrets.get("GITHUB_TOKEN") != "private"
    with pytest.raises(ValueError, match="operator credential"):
        create_app(replace(config, api_key=agent))


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PUT", "/api/config/agents.default_runtime", {"value": "shell"}),
        ("POST", "/api/skills", {"path": "/tmp/skill", "tags": []}),
        ("POST", "/api/signing/owner", {"telegram_user_id": 123}),
        ("DELETE", "/api/agents/worker", None),
    ],
)
async def test_agent_key_cannot_change_administrative_settings(
    api_client, api_app, method, path, body
):
    key = agent_api_key(api_app.state.config.api_key)
    response = await api_client.request(
        method, path, json=body, headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 403


async def test_agent_can_read_and_start_saved_peer_but_cannot_widen_it(api_client, api_app):
    spec = next(iter(api_app.state.config.agents))
    headers = {"Authorization": f"Bearer {agent_api_key(api_app.state.config.api_key)}"}
    response = await api_client.get("/api/config/agents.default_runtime", headers=headers)
    assert response.status_code == 200
    with patch(
        "agent_backbone.api.routes.agents.start_resolved",
        return_value=StartResult(ok=True, already_running=True),
    ) as start:
        response = await api_client.post(f"/api/agents/{spec.name}/start", json={}, headers=headers)
        assert response.status_code == 200
        start.assert_awaited_once()
        for changes in (
            {"runtime": "shell"},
            {"dir": "/tmp/another"},
            {"dir": ""},
            {"tags": ["admin"]},
        ):
            response = await api_client.post(
                f"/api/agents/{spec.name}/start", json=changes, headers=headers
            )
            assert response.status_code == 403
        assert start.await_count == 1
    response = await api_client.patch(
        f"/api/agents/{spec.name}", json={"env": {"HOME": "/tmp/other"}}, headers=headers
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_registration_credential_can_select_own_runtime_and_model(
    api_client, api_app, runtime
):
    store = api_app.state.agent_store
    spec = next(iter(store.agents))
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    headers = {"Authorization": f"Bearer {key}"}
    with patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=True):
        result = await api_client.post(
            f"/api/agents/{spec.name}/restart",
            headers=headers,
            json={"runtime": runtime, "model": "example-model", "from_entity": "forged"},
        )
    assert result.status_code == 200, result.text
    row = await api_app.state.db.transitions.get(result.json()["id"])
    assert row["registration_id"] == spec.registration_id
    assert row["requested_by"] == spec.name
    assert row["runtime"] == runtime and row["model"] == "example-model"
    # The same credential still cannot widen administrative settings.
    denied = await api_client.patch(
        f"/api/agents/{spec.name}", headers=headers, json={"unattended": True}
    )
    assert denied.status_code == 403


@pytest.mark.parametrize("credential", ["shared", "peer", "forged", "retired"])
@pytest.mark.parametrize("claim_headers", [False, True])
async def test_self_switch_cannot_be_claimed_by_headers(
    api_client, api_app, credential, claim_headers
):
    store, db = api_app.state.agent_store, api_app.state.db
    spec, peer = list(store.agents)[:2]
    operator = api_app.state.config.api_key
    if credential == "shared":
        key = agent_api_key(operator)
    elif credential == "peer":
        key = agent_api_key(operator, peer.registration_id)
    elif credential == "forged":
        key = agent_api_key(operator, peer.registration_id).replace(
            peer.registration_id, spec.registration_id
        )
    else:
        key = agent_api_key(operator, "f" * 32)
    headers = {"Authorization": f"Bearer {key}"}
    if claim_headers:
        headers.update(
            {
                "X-Backbone-Client-Agent": spec.name,
                "X-Backbone-Client-Registration": spec.registration_id,
            }
        )
    response = await api_client.post(
        f"/api/agents/{spec.name}/restart",
        headers=headers,
        json={"runtime": spec.runtime, "from_entity": spec.name},
    )
    assert response.status_code in (401, 403)
    assert await db.transitions.pending() == []


async def test_self_switch_clears_previous_runtime_unattended_permission(api_client, api_app):
    from unittest.mock import AsyncMock

    from agent_backbone.services.jobs.transitions import run_transitions

    store = api_app.state.agent_store
    spec = next(iter(store.agents))
    spec = await store.update(spec.name, runtime="codex", unattended=True)
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    headers = {"Authorization": f"Bearer {key}"}
    with patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=True):
        response = await api_client.post(
            f"/api/agents/{spec.name}/restart", headers=headers, json={"runtime": "claude"}
        )
    assert response.status_code == 200
    with (
        patch("agent_backbone.services.jobs.transitions.launch.stop_agent", return_value=True),
        patch(
            "agent_backbone.services.jobs.transitions.start_resolved",
            new_callable=AsyncMock,
            return_value=StartResult(ok=True, ready="ready"),
        ) as launch,
    ):
        await run_transitions(lambda: api_app.state.config, store, api_app.state.db)
    assert launch.await_args.args[2].unattended is False
    assert store.agents.get(spec.name).runtime == "claude"
    row = await api_app.state.db.transitions.get(response.json()["id"])
    assert row["status"] == "completed" and row["result"]["runtime"] == "claude"


async def test_retired_registration_token_cannot_use_a_recreated_name(api_client, api_app):
    store = api_app.state.agent_store
    spec = next(iter(store.agents))
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    await api_app.state.db.agents.delete(spec.name)
    await store.register(replace(spec, registration_id=""))
    assert store.agents.get(spec.name).registration_id != spec.registration_id
    response = await api_client.get("/api/runtimes", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 403
    assert "retired" in response.json()["detail"]


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("selection", ["omitted", "runtime", "model"])
async def test_own_restart_accepts_omitted_and_explicit_saved_selection(
    api_client, api_app, runtime, selection
):
    store = api_app.state.agent_store
    spec = next(iter(store.agents))
    spec = await store.update(spec.name, runtime=runtime, model="saved-model")
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    body = {} if selection == "omitted" else {selection: getattr(spec, selection)}
    with patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=True):
        response = await api_client.post(
            f"/api/agents/{spec.name}/restart",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
    assert response.status_code == 200, response.text
    row = await api_app.state.db.transitions.get(response.json()["id"])
    assert row["registration_id"] == spec.registration_id


async def test_restart_acceptance_does_not_wait_for_an_in_flight_launch(api_client, api_app):
    spec = next(iter(api_app.state.agent_store.agents))
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    locked, release = asyncio.Event(), asyncio.Event()

    async def starting_agent():
        async with lifecycle_lock(spec.name):
            locked.set()
            await release.wait()

    task = asyncio.create_task(starting_agent())
    await locked.wait()
    try:
        with patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=True):
            response = await asyncio.wait_for(
                api_client.post(
                    f"/api/agents/{spec.name}/restart",
                    json={"runtime": spec.runtime},
                    headers={"Authorization": f"Bearer {key}"},
                ),
                timeout=1,
            )
        assert response.status_code == 200, response.text
        row = await api_app.state.db.transitions.get(response.json()["id"])
        assert row["registration_id"] == spec.registration_id
        assert row["stopped_at"] is None
    finally:
        release.set()
        await task
