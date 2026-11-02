"""Reservation metadata cannot silently start an unbound serving provider."""

import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agent_backbone.api.app import create_app, lifespan
from agent_backbone.config import PROFILE_REGISTRY_PATH
from agent_backbone.services.database import (
    BackboneDB,
    ProfileIdentity,
    ProfileRegistry,
    ProfileReservation,
    check_profile_startup,
)


@pytest.fixture
async def db(config):
    async with BackboneDB.connect(config.database_url) as database:
        yield database


async def test_unadopted_startup_without_registry_does_not_allocate_identity(db, tmp_path):
    path = Path(PROFILE_REGISTRY_PATH).expanduser()
    await check_profile_startup(db.url, tmp_path / "legacy")
    assert not path.parent.exists()
    assert await db.profile_identity.read() is None
    async with db.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM signing_install")) == 0


@pytest.mark.parametrize("state", ["creating", "failed"])
async def test_pre_identity_restore_cannot_serve_reserved_tree(db, tmp_path, state):
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    directory = tmp_path / "reserved"
    identity = ProfileIdentity("profile-a", "installation-a", "lifecycle-a1")
    registry.reserve(ProfileReservation(identity, directory, "Work", "baseline-v1"))
    if state == "failed":
        registry.invalidate(identity, directory)
    for path in (directory, directory / "child", tmp_path):
        with pytest.raises(ValueError, match="reserved profile"):
            await check_profile_startup(db.url, path)
    await check_profile_startup(db.url, tmp_path / "legacy")


@pytest.mark.parametrize(
    "condition", ["missing", "matching", "invalidated", "copied", "mismatched", "corrupt"]
)
async def test_reserved_identity_never_activates_on_startup(db, tmp_path, condition):
    identity = ProfileIdentity("profile-a", await db.signing.audience(), "lifecycle-a1")
    await db.profile_identity.reserve(identity)
    path = Path(PROFILE_REGISTRY_PATH).expanduser()
    directory = tmp_path / "provider"
    if condition != "missing":
        registry = ProfileRegistry.create(path)
        stored = (
            replace(identity, lifecycle_id="new-lifecycle")
            if condition == "mismatched"
            else identity
        )
        registry.reserve(ProfileReservation(stored, directory, "Work", "baseline-v1"))
        if condition == "invalidated":
            registry.invalidate(identity, directory)
        elif condition == "corrupt":
            path.write_bytes(b"invalid registry")
    with pytest.raises(ValueError):
        await check_profile_startup(
            db.url, tmp_path / "copy" if condition == "copied" else directory
        )
    assert await db.profile_identity.read() == identity


async def test_corrupt_registry_blocks_unadopted_fallback(db, tmp_path):
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    registry.path.write_bytes(b"invalid registry")
    with pytest.raises(ValueError, match="unavailable"):
        await check_profile_startup(db.url, tmp_path / "legacy")


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_lifespan_refuses_before_storage_or_services(config, runtime):
    async with BackboneDB.connect(config.database_url) as db:
        await db.agents.upsert(
            "worker",
            dir=str(config.data_dir),
            runtime=runtime,
            model=None,
            repo="",
            tags=[],
            env={},
            description="",
            unattended=False,
        )
        await db.profile_identity.reserve(
            ProfileIdentity("profile-a", await db.signing.audience(), "lifecycle-a1")
        )
    app = create_app(config).other_asgi_app
    with (
        patch("agent_backbone.services.database.BackboneDB") as database,
        patch("agent_backbone.services.agents.AgentStore") as agents,
        patch("agent_backbone.api.socketio_server.configure_pty_manager") as pty,
        patch("agent_backbone.hooks.install.install_hook_files") as hooks,
        patch("agent_backbone.api.app._register_jobs") as jobs,
        patch("agent_backbone.services.integrations.build_integrations") as integrations,
    ):
        with pytest.raises(ValueError):
            async with lifespan(app):
                pytest.fail("reserved provider started")
    for effect in (database, agents, pty, hooks, jobs, integrations):
        effect.assert_not_called()


async def test_unadopted_lifespan_reaches_agent_startup(config):
    # Stop at the first service boundary so this unit test starts no jobs.
    app = create_app(config).other_asgi_app
    with patch(
        "agent_backbone.services.agents.AgentStore", side_effect=RuntimeError("reached agents")
    ):
        try:
            with pytest.raises(RuntimeError, match="reached agents"):
                async with lifespan(app):
                    pass
        finally:
            await app.state.db.stop()


@pytest.mark.parametrize("existing", [False, True])
async def test_refused_tree_is_unchanged_before_daemon_or_direct_start(config, existing):
    from agent_backbone.cli._common import Direct

    directory = config.data_dir / "reserved" / "child"
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    registry.reserve(
        ProfileReservation(
            ProfileIdentity("profile-a", "installation-a", "lifecycle-a1"),
            directory.parent,
            "Work",
            "baseline-v1",
        )
    )
    config = replace(config, backbone=replace(config.backbone, data_dir=str(directory)))
    database = directory / "backbone.db"
    if existing:
        directory.mkdir(parents=True)
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE old_backup (value TEXT)")
        before = database.read_bytes(), directory.stat().st_mode
    app = create_app(config).other_asgi_app
    for context in (lifespan(app), Direct(config)):
        with pytest.raises(ValueError, match="reserved profile"):
            async with context:
                pytest.fail("reserved tree opened")
    if existing:
        assert (database.read_bytes(), directory.stat().st_mode) == before
    else:
        assert not directory.exists()


@pytest.mark.parametrize("uri", [False, True])
async def test_copied_identity_is_refused_without_migration(config, uri):
    from agent_backbone.cli._common import Direct

    path = config.data_dir / "old backup #1.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE profile_identity (id INTEGER)")
        conn.execute("INSERT INTO profile_identity VALUES (1)")
    before = path.read_bytes(), path.stat().st_mtime_ns
    url = (
        f"sqlite+aiosqlite:///{path.as_uri()}?mode=rw&uri=true"
        if uri
        else f"sqlite+aiosqlite:///{path}"
    )
    config = replace(config, database_url_override=url)
    with pytest.raises(ValueError, match="not ready"):
        async with Direct(config):
            pytest.fail("copied database opened")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


async def test_agent_start_direct_fallback_refuses_reserved_identity(config, db):
    from agent_backbone.cli import build_parser
    from agent_backbone.cli.agents import _agent_start

    await db.profile_identity.reserve(
        ProfileIdentity("profile-a", await db.signing.audience(), "lifecycle-a1")
    )
    args = build_parser().parse_args(["agent", "start", "worker", "--dir", str(config.data_dir)])
    with (
        patch("agent_backbone.cli._common.read_client_config", AsyncMock(return_value=config)),
        patch("agent_backbone.cli._common.api_up", AsyncMock(return_value=False)),
        patch("agent_backbone.services.agents.operations.start_resolved", AsyncMock()) as start,
    ):
        with pytest.raises(ValueError, match="not ready"):
            await _agent_start(args)
    start.assert_not_awaited()
