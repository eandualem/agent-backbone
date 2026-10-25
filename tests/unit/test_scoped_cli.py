from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_backbone.cli import main
from agent_backbone.cli._common import Direct
from agent_backbone.config import agent_api_key


@pytest.mark.parametrize(
    "argv",
    [
        ["templates", "edit", "base"],
        ["skills", "add", "/tmp/skill", "--local"],
        ["secrets", "set", "BACKBONE_API_KEY", "replacement"],
        ["up"],
        ["doctor"],
    ],
)
def test_agent_cli_refuses_local_administration_before_dispatch(monkeypatch, argv):
    monkeypatch.setenv("BACKBONE_API_KEY", agent_api_key("operator"))
    with pytest.raises(SystemExit) as result:
        main(argv)
    assert result.value.code == 1


async def test_agent_cli_cannot_use_direct_database_fallback(config):
    scoped = replace(config, api_key=agent_api_key("operator"))
    with patch("agent_backbone.services.database.BackboneDB") as database:
        with pytest.raises(ValueError, match="direct database fallback is disabled"):
            async with Direct(scoped):
                pytest.fail("agent opened the database")
    database.assert_not_called()


async def test_agent_client_uses_exported_address_without_operator_env(tmp_path, monkeypatch):
    from agent_backbone.cli._common import read_client_config

    (tmp_path / ".env").write_text("BACKBONE_PORT=9999\nBACKBONE_API_KEY=operator-secret\n")
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BACKBONE_API_KEY", agent_api_key("operator"))
    monkeypatch.setenv("BACKBONE_PORT", "8234")
    monkeypatch.setenv("BACKBONE_HOST", "127.0.0.2")
    config = await read_client_config()
    assert config.backbone.port == 8234
    assert config.backbone.host == "127.0.0.2"
    assert config.api_key == agent_api_key("operator")


@pytest.mark.parametrize("credential", [agent_api_key("operator"), ""])
async def test_scoped_discovery_reads_remote_configuration_without_database(
    tmp_path, monkeypatch, credential
):
    from agent_backbone.cli import _common

    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BACKBONE_AGENT_CREDENTIAL", credential)
    monkeypatch.delenv("BACKBONE_API_KEY", raising=False)
    monkeypatch.setenv("BACKBONE_HOST", "127.0.0.2")
    monkeypatch.setenv("BACKBONE_PORT", "8234")
    (tmp_path / ".env").write_text("BACKBONE_DATABASE_URL=postgresql+asyncpg://private/server\n")

    async def respond(config, method, path):
        assert config.api_key == credential
        assert config.backbone.host == "127.0.0.2"
        assert config.backbone.port == 8234
        return (
            200,
            {"backbone.port": {"value": 8234}}
            if path == "/api/config"
            else [{"name": "worker", "dir": str(tmp_path), "runtime": "codex", "always_on": True}],
        )

    with (
        patch.object(_common, "api", side_effect=respond),
        patch.object(_common, "Direct") as direct,
    ):
        config = await _common.read_config()
    assert config.agents.get("worker").always_on
    assert config.backbone.port == 8234
    direct.assert_not_called()


@pytest.mark.parametrize("command", ["list", "get"])
async def test_scoped_configuration_reads_use_api(config, command, capsys):
    from argparse import Namespace
    from unittest.mock import AsyncMock

    from agent_backbone.cli import _common
    from agent_backbone.cli.server import _config_cmd

    scoped = replace(config, api_key=agent_api_key("operator"))
    with (
        patch.object(_common, "read_client_config", AsyncMock(return_value=scoped)),
        patch.object(
            _common, "api", AsyncMock(return_value=(200, {"backbone.port": {"value": 8234}}))
        ),
        patch.object(_common, "Direct") as direct,
    ):
        assert await _config_cmd(Namespace(config_command=command, key="backbone.port")) == 0
    assert "8234" in capsys.readouterr().out
    direct.assert_not_called()


async def test_scoped_skill_preview_uses_server_environment(config, capsys):
    from argparse import Namespace
    from unittest.mock import AsyncMock

    from agent_backbone.cli import _common, skills

    scoped = replace(config, api_key=agent_api_key("operator"))
    spec = next(iter(scoped.agents))
    expected = {"agent": spec.name, "directories": ["server-selected"], "skills": []}
    with (
        patch.object(_common, "read_config", AsyncMock(return_value=scoped)),
        patch.object(_common, "api", AsyncMock(return_value=(200, expected))) as api,
        patch.object(skills, "skills_preview") as local,
    ):
        # The store setting normally comes from the API configuration snapshot.
        scoped = replace(scoped, skills=replace(scoped.skills, store="/tmp/skills"))
        _common.read_config.return_value = scoped
        assert (
            await skills._skills(Namespace(skills_command="preview", agent=spec.name, json=True))
            == 0
        )
    api.assert_awaited_once_with(scoped, "GET", f"/api/skills/preview/{spec.name}")
    local.assert_not_called()
    assert "server-selected" in capsys.readouterr().out


@pytest.mark.parametrize("credential", [None, "invalid-credential"])
def test_missing_or_invalid_agent_credential_never_reads_operator_file(
    tmp_path, monkeypatch, credential
):
    from agent_backbone.config import load_secrets

    (tmp_path / ".env").write_text("BACKBONE_API_KEY=operator-secret\n")
    monkeypatch.setenv("BACKBONE_AGENT", "app")
    monkeypatch.delenv("BACKBONE_API_KEY", raising=False)
    if credential is None:
        monkeypatch.delenv("BACKBONE_AGENT_CREDENTIAL", raising=False)
    else:
        monkeypatch.setenv("BACKBONE_AGENT_CREDENTIAL", credential)
    with patch("agent_backbone.config.dotenv_values") as read:
        with pytest.raises(ValueError, match="fallback is disabled"):
            load_secrets(tmp_path)
    read.assert_not_called()


def test_explicit_empty_launch_credential_keeps_no_key_client_without_env_fallback(
    tmp_path, monkeypatch
):
    from agent_backbone.config import bootstrap_config

    (tmp_path / ".env").write_text("BACKBONE_API_KEY=operator-secret\n")
    monkeypatch.setenv("BACKBONE_AGENT", "app")
    monkeypatch.setenv("BACKBONE_AGENT_CREDENTIAL", "")
    monkeypatch.delenv("BACKBONE_API_KEY", raising=False)
    with patch("agent_backbone.config.dotenv_values") as read:
        assert bootstrap_config(tmp_path).api_key == ""
    read.assert_not_called()


async def test_empty_launch_credential_cannot_use_direct_database_fallback(config, monkeypatch):
    monkeypatch.setenv("BACKBONE_AGENT_CREDENTIAL", "")
    with patch("agent_backbone.services.database.BackboneDB") as database:
        with pytest.raises(ValueError, match="direct database fallback is disabled"):
            async with Direct(replace(config, api_key="")):
                pytest.fail("agent opened the database")
    database.assert_not_called()
