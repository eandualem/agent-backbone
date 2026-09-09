"""Address-only clients use the server precedence without repairing the database."""

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.cli._common import read_client_config
from agent_backbone.config import AgentsConfig, build_config


@pytest.mark.parametrize(
    "process_port,file_port", [(None, None), ("8123", None), (None, "8234"), ("8123", "8234")]
)
async def test_client_address_matches_server_and_keeps_database_unchanged(
    tmp_path, monkeypatch, process_port, file_port
):
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BACKBONE_PORT", raising=False)
    db_path = tmp_path / "existing.db"
    monkeypatch.setenv("BACKBONE_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    if process_port:
        monkeypatch.setenv("BACKBONE_PORT", process_port)
    if file_port:
        (tmp_path / ".env").write_text(f"BACKBONE_PORT={file_port}\n")
    settings = {"backbone.port": 8345, "backbone.host": "127.0.0.2"}
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO settings VALUES (?, ?)", [(k, json.dumps(v)) for k, v in settings.items()]
        )
    before = db_path.read_bytes(), db_path.stat().st_mtime_ns
    client = await read_client_config()
    server = build_config(tmp_path, settings=settings, agents=AgentsConfig())
    assert client.backbone.port == server.backbone.port == int(process_port or file_port or 8345)
    assert client.backbone.host == server.backbone.host == "127.0.0.2"
    assert (db_path.read_bytes(), db_path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("command", ["status", "tell", "inbox", "swarm"])
async def test_api_commands_do_not_initialize_or_repair_database(
    tmp_path, monkeypatch, existing, command
):
    from agent_backbone.cli import build_parser
    from agent_backbone.cli.agents import _inbox, _tell
    from agent_backbone.cli.status import snapshot
    from agent_backbone.cli.swarms import _swarm

    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    database = tmp_path / "backbone.db"
    monkeypatch.setenv("BACKBONE_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    if existing:
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO settings VALUES ('backbone.port', '7444')")
    before = database.read_bytes() if existing else None
    calls = {
        "status": (snapshot, ["status"]),
        "tell": (_tell, ["tell", "app", "hello"]),
        "inbox": (_inbox, ["inbox", "--agent", "app"]),
        "swarm": (_swarm, ["swarm", "list"]),
    }
    handler, argv = calls[command]
    with patch(
        "agent_backbone.cli._common.api",
        AsyncMock(return_value=(200, {"ok": True, "items": []})),
    ) as api:
        await handler(build_parser().parse_args(argv))
    assert api.await_count > 0
    assert database.exists() == existing
    if existing:
        assert database.read_bytes() == before


async def test_offline_status_uses_registered_agents_and_settings(tmp_path, monkeypatch):
    from agent_backbone.cli import _common, build_parser
    from agent_backbone.cli.status import snapshot
    from agent_backbone.config import AgentSpec

    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BACKBONE_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'backbone.db'}")
    async with _common.Direct(await read_client_config()) as direct:
        await direct.store.register(AgentSpec(name="saved", dir=str(tmp_path), runtime="shell"))
        await direct.db.settings.set("backbone.session_name", "custom-backbone")

    async def build(config):
        assert config.agents.get("saved").runtime == "shell"
        assert config.backbone.session_name == "custom-backbone"
        return []

    with (
        patch("agent_backbone.cli._common.api", AsyncMock(return_value=None)),
        patch("agent_backbone.api.session_updates.build_session_snapshot", side_effect=build),
    ):
        result = await snapshot(build_parser().parse_args(["status"]))
    assert result["api_online"] is False


async def test_offline_stop_protects_configured_backbone_session(tmp_path, monkeypatch, capsys):
    from agent_backbone.cli import _common, build_parser
    from agent_backbone.cli.agents import _agent

    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BACKBONE_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'backbone.db'}")
    async with _common.Direct(await read_client_config()) as direct:
        await direct.db.settings.set("backbone.session_name", "custom-backbone")
    with (
        patch("agent_backbone.cli._common.api_up", AsyncMock(return_value=False)),
        patch("agent_backbone.services.agents.operations.launch.stop_agent", AsyncMock()) as stop,
    ):
        assert await _agent(build_parser().parse_args(["agent", "stop", "custom-backbone"])) == 1
    stop.assert_not_awaited()
    assert "refusing to stop" in capsys.readouterr().out


def test_unreadable_roster_reports_error_without_repair(tmp_path, monkeypatch, capsys):
    from agent_backbone.cli import build_parser
    from agent_backbone.cli.agents import cmd_agent

    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    database = tmp_path / "backbone.db"
    monkeypatch.setenv("BACKBONE_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    before = database.read_bytes()
    with patch("agent_backbone.cli._common.api_up", AsyncMock(return_value=False)):
        assert cmd_agent(build_parser().parse_args(["agent", "list"])) == 1
    assert "Could not read existing configuration" in capsys.readouterr().out
    assert database.read_bytes() == before
