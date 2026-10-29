"""Only the installation operator can inspect identity or execute setup."""

from unittest.mock import AsyncMock

import pytest

from agent_backbone.api.routes import onboarding
from agent_backbone.config import agent_api_key
from agent_backbone.models import SETUP_BODY_BYTES


async def test_identity_is_the_configured_installation(api_client, config, auth_headers):
    response = await api_client.get("/api/setup/identity", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"schema_version": 1, "data_dir": str(config.data_dir.resolve())}


@pytest.mark.parametrize(
    "path,method", [("/api/setup/identity", "GET"), ("/api/setup/first-agent", "POST")]
)
async def test_agent_credential_cannot_run_setup(api_client, config, path, method, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(onboarding, "first_agent", execute)
    response = await api_client.request(
        method, path, json={}, headers={"Authorization": "Bearer " + agent_api_key(config.api_key)}
    )
    assert response.status_code == 403
    execute.assert_not_awaited()


async def test_setup_body_is_bounded_and_invalid_values_are_not_echoed(api_client, auth_headers):
    response = await api_client.post(
        "/api/setup/first-agent", headers=auth_headers, content=b"x" * (SETUP_BODY_BYTES + 1)
    )
    assert response.status_code == 413
    response = await api_client.post(
        "/api/setup/first-agent", headers=auth_headers, json={"secret": "must-not-echo-this-value"}
    )
    assert response.status_code == 422
    assert "must-not-echo-this-value" not in response.text
