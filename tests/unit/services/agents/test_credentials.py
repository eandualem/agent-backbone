"""Shared CLI pickup for claude, codex, opencode, gemini, deepcode, aider and shell.

These tests exercise no runtime process or paid model call.
"""

import asyncio
import multiprocessing
import os
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.cli._common import headers, read_client_config
from agent_backbone.config import (
    AgentSpec,
    agent_api_key,
    agent_profile_path,
    bootstrap_config,
    load_secrets,
    read_agent_profile,
)
from agent_backbone.services.agents import AgentStore, write_starting_marker
from agent_backbone.services.agents.credentials import (
    _bind_agent_profile,
    _provision_agent_profiles,
    agent_profile_readiness,
    profile_lock,
)
from agent_backbone.services.runtimes import RUNTIMES


async def provision_agent_profiles(config, registrations):
    async with profile_lock(config.data_dir):
        _provision_agent_profiles(config, registrations)


async def bind_agent_profile(config, registration, launch_id):
    async with profile_lock(config.data_dir):
        _bind_agent_profile(config, registration, launch_id)


async def registered(db, tmp_path, monkeypatch, runtime="codex"):
    monkeypatch.setenv("BACKBONE_API_KEY", "operator")
    monkeypatch.setattr(
        "agent_backbone.services.terminal.session_exists", AsyncMock(return_value=False)
    )
    boot = bootstrap_config(tmp_path)
    store = AgentStore(db, tmp_path, credential_endpoint=(boot.backbone.host, boot.backbone.port))
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
        await provision_agent_profiles(rotated, rows)
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
    await provision_agent_profiles(current, await db.agents.list())
    row = next(row for row in await db.agents.list() if row["name"] == "worker")
    await bind_agent_profile(stale, row, "b" * 32)
    worker = read_agent_profile(tmp_path, "worker")
    peer = read_agent_profile(tmp_path, "peer")
    assert worker["launch"] == "b" * 32
    assert worker["credential"] == peer["credential"] == agent_api_key("rotated")


async def test_local_configuration_reader_cannot_replace_daemon_endpoint(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKBONE_PORT", "8234")
    daemon = await registered(db, tmp_path, monkeypatch)
    launch = "a" * 32
    write_starting_marker(daemon.config.state_dir, "worker", time.time(), launch_id=launch)
    await daemon.refresh()
    # Direct uses the default store from an operator terminal whose environment
    # need not match the running service's launch environment.
    monkeypatch.delenv("BACKBONE_PORT")
    local_reader = AgentStore(db, tmp_path)
    await local_reader.refresh()
    assert local_reader.config.backbone.port != 8234
    assert read_agent_profile(tmp_path, "worker")["port"] == "8234"
    monkeypatch.setenv("BACKBONE_AGENT", "worker")
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", launch)
    monkeypatch.setenv("BACKBONE_STATE_DIR", str(daemon.config.state_dir))
    monkeypatch.delenv("BACKBONE_API_KEY")
    assert (await read_client_config()).backbone.port == 8234


async def test_offline_recreated_registration_can_start_without_restoring_old_identity(
    db, tmp_path, monkeypatch
):
    daemon = await registered(db, tmp_path, monkeypatch)
    old = read_agent_profile(tmp_path, "worker")
    await daemon.forget("worker")
    # With no daemon, Direct can register/start through its local operator store.
    offline = AgentStore(db, tmp_path)
    await offline.start()
    spec = await offline.register(AgentSpec(name="worker", dir=str(tmp_path), runtime="shell"))
    with (
        patch("agent_backbone.services.agents.launch.session_exists", return_value=False),
        patch("agent_backbone.services.agents.launch.start_session", return_value=True) as start,
    ):
        from agent_backbone.services.agents import start_agent

        assert (await start_agent(spec, offline.config, db=db, wait=False)).ok
    profile = read_agent_profile(tmp_path, "worker")
    row = next(row for row in await db.agents.list() if row["name"] == "worker")
    assert profile["registration"] == row["report_identity"] != old["registration"]
    assert profile["launch"] == start.call_args.kwargs["environment"]["BACKBONE_LAUNCH_ID"]
    assert profile["credential"] == agent_api_key("operator")


def _paused_profile_binding(config, registration, entered, release):
    from agent_backbone.services.agents import credentials

    write = credentials._write_profile

    def paused_write(*args):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("profile binding was not released")
        write(*args)

    with patch.object(credentials, "_write_profile", side_effect=paused_write):
        asyncio.run(bind_agent_profile(config, registration, "b" * 32))


async def test_daemon_rotation_serializes_with_a_separate_cli_binding(db, tmp_path, monkeypatch):
    store = await registered(db, tmp_path, monkeypatch)
    rows = await db.agents.list()
    row = next(row for row in rows if row["name"] == "worker")
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(
        target=_paused_profile_binding, args=(store.config, row, entered, release)
    )
    process.start()
    rotation = None
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        rotated = replace(
            store.config, api_key="rotated", backbone=replace(store.config.backbone, port=8234)
        )
        rotation = asyncio.create_task(provision_agent_profiles(rotated, rows))
        await asyncio.sleep(0.1)
        assert not rotation.done()
        release.set()
        await asyncio.wait_for(rotation, 10)
        await asyncio.to_thread(process.join, 10)
        assert process.exitcode == 0
        profile = read_agent_profile(tmp_path, "worker")
        assert profile["credential"] == agent_api_key("rotated")
        assert profile["port"] == "8234"
    finally:
        release.set()
        if rotation is not None:
            await asyncio.wait_for(rotation, 10)
        await asyncio.to_thread(process.join, 10)
        if process.is_alive():
            process.kill()
            process.join()


@pytest.mark.parametrize("change", ["register", "rename", "replace"])
async def test_refresh_snapshot_cannot_overwrite_a_newer_cli_registration(
    db, tmp_path, monkeypatch, change
):
    daemon = await registered(db, tmp_path, monkeypatch)
    direct = AgentStore(db, tmp_path)
    await direct.start()
    captured, release = asyncio.Event(), asyncio.Event()
    list_agents = db.agents.list

    async def delayed_snapshot():
        rows = await list_agents()
        if not captured.is_set():
            captured.set()
            await release.wait()
        return rows

    name = "worker" if change == "replace" else "peer"

    async def mutate_and_bind():
        if change == "rename":
            await direct.rename("worker", name)
        else:
            if change == "replace":
                await direct.forget(name)
            await direct.register(AgentSpec(name=name, dir=str(tmp_path)))
        row = next(row for row in await list_agents() if row["name"] == name)
        await bind_agent_profile(direct.config, row, "b" * 32)
        return row

    mutation = None
    with patch.object(db.agents, "list", side_effect=delayed_snapshot):
        refresh = asyncio.create_task(daemon.refresh())
        try:
            await asyncio.wait_for(captured.wait(), 5)
            mutation = asyncio.create_task(mutate_and_bind())
            await asyncio.sleep(0.1)
            assert not mutation.done()
        finally:
            release.set()
            await asyncio.wait_for(refresh, 5)
            if mutation is not None:
                row = await asyncio.wait_for(mutation, 5)
    profile = read_agent_profile(tmp_path, name)
    assert profile["registration"] == row["report_identity"]
    assert profile["launch"] == "b" * 32


async def test_concurrent_starts_do_not_rebind_the_surviving_session(db, tmp_path, monkeypatch):
    from agent_backbone.services.agents import start_agent

    store = await registered(db, tmp_path, monkeypatch, runtime="shell")
    spec = store.agents.get("worker")
    both_started = asyncio.Event()
    checks = 0
    live_environment = None

    async def exists(_name):
        nonlocal checks
        if checks < 2:
            checks += 1
            if checks == 2:
                both_started.set()
            await both_started.wait()
            return False
        return live_environment is not None

    async def create(_name, **kwargs):
        nonlocal live_environment
        # start_session retains an existing session and its original environment.
        if live_environment is None:
            live_environment = kwargs["environment"]
        return True

    with (
        patch("agent_backbone.services.agents.launch.session_exists", side_effect=exists),
        patch("agent_backbone.services.agents.launch.start_session", side_effect=create) as start,
    ):
        results = await asyncio.wait_for(
            asyncio.gather(
                start_agent(spec, store.config, db=db, wait=False),
                start_agent(spec, store.config, db=db, wait=False),
            ),
            5,
        )
    assert all(result.ok for result in results)
    assert sum(result.already_running for result in results) == 1
    assert start.await_count == 1
    await store.refresh()
    for key, value in live_environment.items():
        monkeypatch.setenv(key, value)
    assert load_secrets(tmp_path)["BACKBONE_API_KEY"] == agent_api_key("operator")


async def test_resumed_launch_cannot_overwrite_its_replacement_binding(db, tmp_path, monkeypatch):
    from agent_backbone.services.agents import start_agent

    store = await registered(db, tmp_path, monkeypatch)
    spec = store.agents.get("worker")
    recording, release, replacement_checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    live_environment = None
    record = db.agents.conversation_started

    async def exists(_name):
        if recording.is_set():
            replacement_checked.set()
        return live_environment is not None

    async def create(_name, **kwargs):
        nonlocal live_environment
        live_environment = kwargs["environment"]
        return True

    async def delayed_record(*args):
        if not recording.is_set():
            recording.set()
            await release.wait()
        await record(*args)

    with (
        patch("agent_backbone.services.agents.launch.session_exists", side_effect=exists),
        patch("agent_backbone.services.agents.launch.start_session", side_effect=create),
        patch("agent_backbone.services.runtimes.base.resolve_command", return_value="/bin/codex"),
        patch.object(db.agents, "conversation_started", side_effect=delayed_record),
    ):
        original = asyncio.create_task(
            start_agent(spec, store.config, session_id="saved", db=db, wait=False)
        )
        await asyncio.wait_for(recording.wait(), 5)
        live_environment = None  # The first session exits before its bookkeeping finishes.
        replacement = asyncio.create_task(start_agent(spec, store.config, db=db, wait=False))
        try:
            await asyncio.wait_for(replacement_checked.wait(), 5)
            await asyncio.sleep(0.1)
        finally:
            release.set()
        assert all(result.ok for result in await asyncio.gather(original, replacement))
    await store.refresh()
    for key, value in live_environment.items():
        monkeypatch.setenv(key, value)
    assert load_secrets(tmp_path)["BACKBONE_API_KEY"] == agent_api_key("operator")


async def test_saved_endpoint_change_waits_for_listener_restart(db, tmp_path, monkeypatch):
    store = await registered(db, tmp_path, monkeypatch)
    before = read_agent_profile(tmp_path, "worker")
    await db.settings.set("backbone.port", 8234)
    await db.settings.set("backbone.host", "localhost")
    await store.refresh()
    assert store.config.backbone.port == 8234
    after = read_agent_profile(tmp_path, "worker")
    assert (after["host"], after["port"]) == (before["host"], before["port"])
    restarted = AgentStore(db, tmp_path, credential_endpoint=("localhost", 8234))
    await restarted.start()
    assert read_agent_profile(tmp_path, "worker")["port"] == "8234"


async def test_launch_refuses_registration_replaced_during_preparation(db, tmp_path, monkeypatch):
    from agent_backbone.services.agents import start_agent

    store = await registered(db, tmp_path, monkeypatch, runtime="shell")
    spec = store.agents.get("worker")

    async def replace_registration(*args):
        # A separate Direct process does not share this process's lifecycle lock.
        await db.agents.delete("worker")
        await db.agents.upsert(
            "worker",
            dir=str(tmp_path),
            runtime="shell",
            model=None,
            repo="",
            tags=[],
            env={},
            description="",
            unattended=False,
        )
        return 0

    with (
        patch("agent_backbone.services.agents.launch.session_exists", return_value=False),
        patch(
            "agent_backbone.services.agents.launch._retire_stale_briefs",
            side_effect=replace_registration,
        ),
        patch("agent_backbone.services.agents.launch.start_session", return_value=True) as start,
    ):
        result = await start_agent(spec, store.config, db=db, wait=False)
    assert not result.ok
    assert "registration changed" in " ".join(result.evidence)
    start.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["register", "new", "forget", "rename"])
async def test_registration_mutations_wait_for_session_creation(
    db, tmp_path, monkeypatch, mutation
):
    from agent_backbone.services.agents import start_agent

    store = await registered(db, tmp_path, monkeypatch, runtime="shell")
    spec = store.agents.get("worker")
    entered, release = asyncio.Event(), asyncio.Event()
    running = False

    async def create(*args, **kwargs):
        nonlocal running
        entered.set()
        await release.wait()
        running = True
        return True

    async def exists(name):
        return running and name == "worker"

    async def mutate():
        if mutation == "forget":
            return await store.forget("worker")
        if mutation == "rename":
            return await store.rename("worker", "renamed")
        return await store.register(replace(spec, name="peer" if mutation == "new" else "worker"))

    with (
        patch("agent_backbone.services.agents.launch.session_exists", side_effect=exists),
        patch("agent_backbone.services.terminal.session_exists", side_effect=exists),
        patch("agent_backbone.services.agents.launch.start_session", side_effect=create) as start,
    ):
        launch = asyncio.create_task(start_agent(spec, store.config, db=db, wait=False))
        await asyncio.wait_for(entered.wait(), 5)
        refresh = asyncio.create_task(store.refresh())
        change = asyncio.create_task(mutate())
        try:
            await asyncio.sleep(0.1)
            assert not change.done()
        finally:
            release.set()
            results = await asyncio.wait_for(
                asyncio.gather(launch, refresh, change, return_exceptions=True), 5
            )
        assert results[0].ok
        assert not isinstance(results[1], BaseException)
        if mutation in {"forget", "rename"}:
            assert isinstance(results[2], (RuntimeError, ValueError))
        else:
            assert not isinstance(results[2], BaseException)
    await store.refresh()
    for key, value in start.call_args.kwargs["environment"].items():
        monkeypatch.setenv(key, value)
    assert load_secrets(tmp_path)["BACKBONE_API_KEY"] == agent_api_key("operator")
