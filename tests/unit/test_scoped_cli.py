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


async def test_scoped_discovery_reads_remote_configuration_without_database(tmp_path, monkeypatch):
    from agent_backbone.cli import _common

    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BACKBONE_API_KEY", agent_api_key("operator"))
    (tmp_path / ".env").write_text("BACKBONE_DATABASE_URL=postgresql+asyncpg://private/server\n")

    async def respond(config, method, path):
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
