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
