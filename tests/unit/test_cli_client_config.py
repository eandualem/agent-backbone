"""Address-only clients use the server precedence without repairing the database."""

import json
import sqlite3

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
