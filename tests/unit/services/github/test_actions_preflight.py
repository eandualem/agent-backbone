"""Actions policy reads use existing authentication and never change permissions."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from agent_backbone.config import BackboneConfig, GitHubConfig
from agent_backbone.services.github import API_BASE, GitHubClient, actions_checker

URL = f"{API_BASE}/repos/acme/app/actions/permissions"


@pytest.mark.parametrize("enabled", [True, False])
@respx.mock
async def test_boolean_permissions_with_intake_off(enabled):
    route = respx.get(URL).respond(json={"enabled": enabled, "allowed_actions": "selected"})
    config = BackboneConfig(github_token="example-token", github=GitHubConfig(intake="off"))
    assert await actions_checker(config)("acme/app") is enabled
    assert route.call_count == 1
    assert route.calls[0].request.headers["Authorization"] == "Bearer example-token"
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    "payload", [{}, {"enabled": None}, {"enabled": 1}, {"enabled": "true"}, [], True]
)
@respx.mock
async def test_malformed_permissions_raise(payload):
    respx.get(URL).respond(json=payload)
    with pytest.raises(ValueError):
        await actions_checker(BackboneConfig(github_token="example-token"))("acme/app")


@pytest.mark.parametrize("status", [401, 403, 404, 500])
@respx.mock
async def test_http_errors_raise(status):
    respx.get(URL).respond(status)
    with pytest.raises(httpx.HTTPStatusError):
        await actions_checker(BackboneConfig(github_token="example-token"))("acme/app")


@respx.mock
async def test_invalid_json_and_network_failure():
    route = respx.get(URL).respond(content=b"not json")
    check = actions_checker(BackboneConfig(github_token="example-token"))
    with pytest.raises(ValueError):
        await check("acme/app")
    route.mock(side_effect=httpx.ConnectError("offline"))
    with pytest.raises(httpx.ConnectError):
        await check("acme/app")


async def test_missing_authentication_fails():
    with pytest.raises(RuntimeError, match="auth not configured"):
        await actions_checker(BackboneConfig())("acme/app")


@respx.mock
async def test_app_authentication_uses_repository_installation():
    from pathlib import Path

    config = BackboneConfig(
        github_app_id=123,
        github_app_private_key_path=str(
            Path(__file__).parents[3] / "fixtures" / "github-app-test-key.pem"
        ),
    )
    route = respx.get(URL).respond(json={"enabled": True})
    with patch.object(
        GitHubClient, "_get_installation_token", AsyncMock(return_value="app-token")
    ) as token:
        assert await actions_checker(config)("acme/app") is True
    token.assert_awaited_once_with("acme", "app")
    assert route.calls[0].request.headers["Authorization"] == "Bearer app-token"
