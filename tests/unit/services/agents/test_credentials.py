"""Shared CLI pickup for claude, codex, opencode, gemini, deepcode, aider and shell.

These tests exercise no runtime process or paid model call.
"""

import os
import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_backbone.cli._common import headers, read_client_config
from agent_backbone.config import (
    AgentSpec,
    agent_api_key,
    agent_profile_path,
    load_secrets,
    read_agent_profile,
)
from agent_backbone.services.agents import AgentStore, write_starting_marker
from agent_backbone.services.agents.credentials import (
    agent_profile_readiness,
    bind_agent_profile,
    provision_agent_profiles,
)
from agent_backbone.services.runtimes import RUNTIMES


async def registered(db, tmp_path, monkeypatch, runtime="codex"):
    monkeypatch.setenv("BACKBONE_API_KEY", "operator")
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="worker", dir=str(tmp_path), runtime=runtime))
    return store


@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_existing_and_offline_agents_pick_up_scoped_profiles_without_restart(
    db, tmp_path, monkeypatch, runtime
):
    store = await registered(db, tmp_path, monkeypatch, runtime)
    rows = await db.agents.list()
    row = next(row for row in rows if row["name"] == "worker")
    assert agent_profile_readiness(store.config, row) == "future-launch-only"
    launch = "a" * 32
    write_starting_marker(store.config.state_dir, "worker", time.time(), launch_id=launch)
    await store.refresh()
    assert agent_profile_readiness(store.config, row) == "ready-existing-launch"
    (tmp_path / ".env").write_text("BACKBONE_API_KEY=operator\nGITHUB_TOKEN=private\n")
    with monkeypatch.context() as client:
        client.setenv("BACKBONE_AGENT", "worker")
        client.setenv("BACKBONE_RUNTIME", runtime)
        client.setenv("BACKBONE_LAUNCH_ID", launch)
        client.delenv("BACKBONE_DATA_DIR", raising=False)
        client.setenv("BACKBONE_STATE_DIR", str(store.config.state_dir))
        client.delenv("BACKBONE_API_KEY")
        client.setenv("BACKBONE_HOST", "attacker.example")
        client.setenv("BACKBONE_PORT", "9999")
        config = await read_client_config()
        assert config.api_key == agent_api_key("operator")
        assert config.github_token == ""
        assert config.backbone.host == "127.0.0.1"
        assert config.backbone.port == store.config.backbone.port
        assert headers(config)["X-Backbone-Client-Registration"] == row["report_identity"]
        rotated = replace(
            store.config, api_key="rotated", backbone=replace(config.backbone, port=8234)
        )
        provision_agent_profiles(rotated, rows)
        client.setenv("BACKBONE_AGENT_CREDENTIAL", config.api_key)
        config = await read_client_config()
        assert config.api_key == agent_api_key("rotated")
        assert config.backbone.port == 8234
    assert os.stat(agent_profile_path(tmp_path, "worker")).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "agent-credentials").st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "damage", ["mode", "symlink", "directory-symlink", "malformed", "endpoint", "launch"]
)
async def test_unsafe_profile_refuses_without_operator_fallback(db, tmp_path, monkeypatch, damage):
    store = await registered(db, tmp_path, monkeypatch)
    launch = "a" * 32
    write_starting_marker(store.config.state_dir, "worker", time.time(), launch_id=launch)
    await store.refresh()
    path = agent_profile_path(tmp_path, "worker")
    if damage == "mode":
        path.chmod(0o644)
    elif damage == "symlink":
        target = tmp_path / "outside.env"
        path.rename(target)
        path.symlink_to(target)
    elif damage == "directory-symlink":
        target = tmp_path / "outside"
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    elif damage == "malformed":
        path.write_text("credential=operator\n")
    elif damage == "endpoint":
        path.write_text(path.read_text().replace("host=127.0.0.1", "host=attacker.example/path"))
    else:
        launch = "b" * 32
    monkeypatch.setenv("BACKBONE_AGENT", "worker")
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", launch)
    monkeypatch.delenv("BACKBONE_API_KEY")
    (tmp_path / ".env").write_text("BACKBONE_API_KEY=operator\n")
    with pytest.raises(ValueError, match="profile"):
        load_secrets(tmp_path)


async def test_forgotten_and_recreated_names_never_adopt_the_old_launch(db, tmp_path, monkeypatch):
    store = await registered(db, tmp_path, monkeypatch)
    launch = "a" * 32
    write_starting_marker(store.config.state_dir, "worker", time.time(), launch_id=launch)
    await store.refresh()
    old_identity = next(row for row in await db.agents.list() if row["name"] == "worker")[
        "report_identity"
    ]
    await store.forget("worker")
    with monkeypatch.context() as client:
        client.setenv("BACKBONE_AGENT", "worker")
        client.setenv("BACKBONE_LAUNCH_ID", launch)
        client.setenv("BACKBONE_API_KEY", agent_api_key("operator"))
        with pytest.raises(ValueError, match="retired"):
            load_secrets(tmp_path)
    await store.register(AgentSpec(name="worker", dir=str(tmp_path)))
    row = next(row for row in await db.agents.list() if row["name"] == "worker")
    assert row["report_identity"] != old_identity
    assert agent_profile_readiness(store.config, row) == "uncertain-legacy"
    with monkeypatch.context() as client:
        client.setenv("BACKBONE_AGENT", "worker")
        client.setenv("BACKBONE_LAUNCH_ID", launch)
        with pytest.raises(ValueError, match="does not match"):
            load_secrets(tmp_path)


async def test_renamed_agent_keeps_registration_but_retires_old_name(db, tmp_path, monkeypatch):
    store = await registered(db, tmp_path, monkeypatch)
    old = next(row for row in await db.agents.list() if row["name"] == "worker")
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        await store.rename("worker", "renamed")
    row = next(row for row in await db.agents.list() if row["name"] == "renamed")
    assert row["report_identity"] == old["report_identity"]
    assert agent_profile_readiness(store.config, row) == "future-launch-only"
    assert agent_profile_path(tmp_path, "worker").read_text() == "retired=true\n"


async def test_registration_header_refuses_a_retired_profile(api_client, api_app):
    row = (await api_app.state.db.agents.list())[0]
    request_headers = {
        "Authorization": "Bearer " + agent_api_key(api_app.state.config.api_key),
        "X-Backbone-Client-Agent": row["name"],
        "X-Backbone-Client-Registration": row["report_identity"],
    }
    assert (await api_client.get("/api/config", headers=request_headers)).status_code == 200
    await api_app.state.db.agents.delete(row["name"])
    response = await api_client.get("/api/config", headers=request_headers)
    assert response.status_code == 403
    assert "retired" in response.json()["detail"]


async def test_explicit_client_without_managed_launch_keeps_its_scoped_credential(
    db, tmp_path, monkeypatch
):
    await registered(db, tmp_path, monkeypatch)
    monkeypatch.setenv("BACKBONE_AGENT", "worker")
    monkeypatch.setenv("BACKBONE_API_KEY", agent_api_key("operator"))
    monkeypatch.delenv("BACKBONE_LAUNCH_ID", raising=False)
    assert load_secrets(tmp_path)["BACKBONE_API_KEY"] == agent_api_key("operator")


async def test_launch_binding_preserves_new_registrations_and_rotated_credentials(
    db, tmp_path, monkeypatch
):
    store = await registered(db, tmp_path, monkeypatch)
    stale = store.config
    # Another registration and key rotation complete after this launch captured
    # its configuration. A launch must not perform fleet-wide reconciliation.
    await store.register(AgentSpec(name="peer", dir=str(tmp_path)))
    current = replace(store.config, api_key="rotated")
    provision_agent_profiles(current, await db.agents.list())
    bind_agent_profile(stale, "worker", "b" * 32)
    worker = read_agent_profile(tmp_path, "worker")
    peer = read_agent_profile(tmp_path, "peer")
    assert worker["launch"] == "b" * 32
    assert worker["credential"] == peer["credential"] == agent_api_key("rotated")
