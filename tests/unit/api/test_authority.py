"""Operator authority stays separate from collaboration credentials.

This API boundary applies equally to claude, codex, opencode, gemini,
deepcode, aider and shell; required-runtime launch exports are tested separately.
"""

from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_backbone.api.app import create_app
from agent_backbone.api.auth import credential_role
from agent_backbone.config import agent_api_key, load_secrets
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
