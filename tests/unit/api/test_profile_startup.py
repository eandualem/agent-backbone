"""Reservation metadata cannot silently start an unbound serving provider."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text

from agent_backbone.api.app import create_app, lifespan
from agent_backbone.api.profile_binding import check_profile_startup
from agent_backbone.config import PROFILE_REGISTRY_PATH
from agent_backbone.services.database import (
    BackboneDB,
    ProfileIdentity,
    ProfileRegistry,
    ProfileReservation,
)


async def test_unadopted_startup_without_registry_does_not_allocate_identity(db, tmp_path):
    path = Path(PROFILE_REGISTRY_PATH).expanduser()
    await check_profile_startup(db, tmp_path / "legacy")
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
            await check_profile_startup(db, path)
    await check_profile_startup(db, tmp_path / "legacy")


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
        await check_profile_startup(db, tmp_path / "copy" if condition == "copied" else directory)
    assert await db.profile_identity.read() == identity


async def test_corrupt_registry_blocks_unadopted_fallback(db, tmp_path):
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    registry.path.write_bytes(b"invalid registry")
    with pytest.raises(ValueError, match="unavailable"):
        await check_profile_startup(db, tmp_path / "legacy")


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_lifespan_refuses_before_services_and_closes_database(config, runtime):
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
    original_stop = BackboneDB.stop
    stopped = []

    async def stop(db):
        stopped.append(db)
        await original_stop(db)

    with (
        patch.object(BackboneDB, "stop", stop),
        patch("agent_backbone.services.agents.AgentStore") as agents,
        patch("agent_backbone.api.socketio_server.configure_pty_manager") as pty,
        patch("agent_backbone.hooks.install.install_hook_files") as hooks,
        patch("agent_backbone.api.app._register_jobs") as jobs,
        patch("agent_backbone.services.integrations.build_integrations") as integrations,
    ):
        with pytest.raises(ValueError):
            async with lifespan(app):
                pytest.fail("reserved provider started")
    assert stopped == [app.state.db]
    for effect in (agents, pty, hooks, jobs, integrations):
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
