"""Operator sessions on a bound provider: shells and CLI sign-in."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import agent_api_key
from agent_backbone.services.runtimes import RUNTIMES, _environment, install_native_environments
from tests.unit.api.test_profile_binding import bound_provider  # noqa: F401


@pytest.fixture
def native(bound_provider, monkeypatch):  # noqa: F811
    provider, _, headers = bound_provider
    root = provider.reservation.data_dir
    monkeypatch.setattr(_environment, "_CONTEXT", None)
    for runtime in ("claude", "codex", "opencode"):
        monkeypatch.setattr(type(RUNTIMES[runtime]), "resolve_binary", lambda self: "/bin/true")
    captured = {
        runtime: RUNTIMES[runtime].capture_environment(root / "native" / runtime)
        for runtime in ("shell", "claude", "codex", "opencode")
    }
    for environment in captured.values():
        environment.create_directories()
    install_native_environments(captured)
    return provider, headers, captured


@pytest.fixture
def terminal():
    with (
        patch("agent_backbone.api.profile_sessions.session_exists", AsyncMock(return_value=False)),
        patch(
            "agent_backbone.api.profile_sessions.start_session", AsyncMock(return_value=True)
        ) as start,
    ):
        yield start


async def test_shell_starts_isolated_in_the_profile_home(
    api_client, auth_headers, native, terminal
):
    provider, headers, captured = native
    response = await api_client.post(
        "/api/profile/shells",
        headers={**auth_headers, **headers},
        json={"schema_version": 1, "shell_id": "0123456789ab", "cwd": None},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["session"] == "shell-0123456789ab"
    assert (body["created"], body["shell_id"]) == (True, "0123456789ab")
    assert body["profile_id"] == provider.reservation.identity.profile_id
    options = terminal.call_args.kwargs
    assert options["working_dir"] == captured["shell"]["HOME"]
    assert options["inherit_environment"] is False
    assert options["environment"]["HOME"] == captured["shell"]["HOME"]


async def test_a_live_shell_is_returned_unchanged(api_client, auth_headers, native, terminal):
    _, headers, _ = native
    with patch("agent_backbone.api.profile_sessions.session_exists", AsyncMock(return_value=True)):
        response = await api_client.post(
            "/api/profile/shells",
            headers={**auth_headers, **headers},
            json={"schema_version": 1, "shell_id": "0123456789ab", "cwd": "/missing-but-ignored"},
        )
    assert response.status_code == 200
    assert response.json()["created"] is False
    terminal.assert_not_called()


@pytest.mark.parametrize("cwd", ["relative/path", "/does/not/exist"])
async def test_invalid_cwd(api_client, auth_headers, native, terminal, cwd):
    _, headers, _ = native
    response = await api_client.post(
        "/api/profile/shells",
        headers={**auth_headers, **headers},
        json={"schema_version": 1, "shell_id": "0123456789ab", "cwd": cwd},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_cwd"
    terminal.assert_not_called()


async def test_terminal_failure_is_shell_unavailable(api_client, auth_headers, native, terminal):
    _, headers, _ = native
    terminal.return_value = False
    response = await api_client.post(
        "/api/profile/shells",
        headers={**auth_headers, **headers},
        json={"schema_version": 1, "shell_id": "0123456789ab", "cwd": None},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "shell_unavailable"


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_sign_in_runs_the_cli_with_only_its_profile_environment(
    api_client, auth_headers, native, terminal, runtime
):
    _, headers, captured = native
    response = await api_client.post(
        "/api/profile/accounts/sign-in",
        headers={**auth_headers, **headers},
        json={"schema_version": 1, "runtime": runtime},
    )
    assert response.status_code == 200
    assert response.json()["session"] == f"sign-in-{runtime}"
    options = terminal.call_args.kwargs
    assert options["environment"] == dict(captured[runtime])
    assert options["inherit_environment"] is False
    assert options["command"][4:] == ["/bin/true", *RUNTIMES[runtime].account_args("login")]


@pytest.mark.parametrize("runtime", ["gemini", "shell", "unknown"])
async def test_sign_in_refuses_other_runtimes(api_client, auth_headers, native, terminal, runtime):
    _, headers, _ = native
    response = await api_client.post(
        "/api/profile/accounts/sign-in",
        headers={**auth_headers, **headers},
        json={"schema_version": 1, "runtime": runtime},
    )
    assert response.status_code == 422
    terminal.assert_not_called()


async def test_agent_credentials_cannot_open_operator_sessions(
    api_client, api_app, native, terminal
):
    spec = await api_app.state.agent_store.update("ike", runtime="codex")
    key = agent_api_key(api_app.state.config.api_key, spec.registration_id)
    for path, body in (
        ("shells", {"schema_version": 1, "shell_id": "0123456789ab", "cwd": None}),
        ("accounts/sign-in", {"schema_version": 1, "runtime": "codex"}),
    ):
        response = await api_client.post(
            f"/api/profile/{path}", headers={"Authorization": f"Bearer {key}"}, json=body
        )
        assert response.status_code == 403
    terminal.assert_not_called()


async def test_refusals_match_the_shared_fixtures(api_client, auth_headers, native, terminal):
    _, headers, _ = native
    sessions = json.loads(Path("docs/contracts/profiles-v1.json").read_text())["sessions"]
    bound = {**auth_headers, **headers}
    shell = {"schema_version": 1, "shell_id": "0123456789ab"}
    for path, body, expected in (
        ("shells", {**shell, "cwd": "/missing"}, sessions["shell_refusals"]["invalid_cwd"]),
        ("shells", shell, sessions["invalid_request"]),
        (
            "accounts/sign-in",
            {"schema_version": 1, "runtime": "gemini"},
            sessions["sign_in_refusals"]["runtime_unavailable"],
        ),
    ):
        response = await api_client.post(f"/api/profile/{path}", headers=bound, json=body)
        assert (response.status_code, response.json()) == (expected["status"], expected["body"])


@pytest.fixture
def adopted(api_app, native):
    provider, headers, _ = native
    api_app.state.profile_provider = replace(
        provider, reservation=replace(provider.reservation, kind="legacy_default")
    )
    with patch("agent_backbone.api.profile_binding.provider_current", AsyncMock(return_value=True)):
        yield headers


@pytest.mark.parametrize("cwd", [None, "/tmp"])
async def test_default_shell_keeps_the_host_environment_without_secrets(
    api_client, auth_headers, api_app, adopted, terminal, cwd
):
    response = await api_client.post(
        "/api/profile/shells",
        headers={**auth_headers, **adopted},
        json={"schema_version": 1, "shell_id": "0123456789ab", "cwd": cwd},
    )
    assert response.status_code == 200
    options = terminal.call_args.kwargs
    assert options["working_dir"] == (cwd or str(Path.home()))
    assert "environment" not in options and "inherit_environment" not in options
    assert "BACKBONE_API_KEY" in options["scrub"]


async def test_default_keeps_its_sign_ins(api_client, auth_headers, adopted, terminal):
    sessions = json.loads(Path("docs/contracts/profiles-v1.json").read_text())["sessions"]
    expected = sessions["sign_in_refusals"]["default_keeps_sign_ins"]
    response = await api_client.post(
        "/api/profile/accounts/sign-in",
        headers={**auth_headers, **adopted},
        json={"schema_version": 1, "runtime": "codex"},
    )
    assert (response.status_code, response.json()) == (expected["status"], expected["body"])
    terminal.assert_not_called()


async def test_legacy_provider_has_no_sessions(api_client, auth_headers):
    response = await api_client.post(
        "/api/profile/shells",
        headers=auth_headers,
        json={"schema_version": 1, "shell_id": "0123456789ab", "cwd": None},
    )
    assert response.status_code == 404
