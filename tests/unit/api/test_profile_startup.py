"""Nobody runs without a profile: direct CLI access and the first-profile commands."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_backbone.api.app import create_app, lifespan
from agent_backbone.cli import _common, build_parser
from agent_backbone.config import PROFILE_REGISTRY_PATH, bootstrap_config
from agent_backbone.services.database import (
    ProfileIdentity,
    ProfileRegistry,
    ProfileReservation,
)


def _registry() -> ProfileRegistry:
    return ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())


def _init(directory: Path):
    args = build_parser().parse_args(["init", "--data-dir", str(directory)])
    return args.func(args)


@pytest.mark.real_profile_check
async def test_direct_refuses_without_a_profile_and_creates_nothing(tmp_path):
    directory = tmp_path / "data"
    with pytest.raises(ValueError, match="run `backbone init` first"):
        async with _common.Direct(bootstrap_config(directory)):
            pass
    assert not directory.exists()


@pytest.mark.real_profile_check
async def test_direct_opens_the_default_profile_but_no_other_directory(tmp_path):
    assert await asyncio.to_thread(_init, tmp_path / "default") == 0
    async with _common.Direct(bootstrap_config(tmp_path / "default")) as direct:
        assert await direct.db.settings.all() is not None
    with pytest.raises(ValueError, match="No profile at"):
        async with _common.Direct(bootstrap_config(tmp_path / "other")):
            pass
    assert not (tmp_path / "other").exists()


@pytest.mark.real_profile_check
async def test_direct_refuses_an_isolated_profiles_directory(tmp_path):
    directory = tmp_path / "isolated"
    identity = ProfileIdentity("profile-a", "installation-a", "lifecycle-a")
    registry = _registry()
    with registry._connection(write=True) as conn:
        registry._reserve(conn, ProfileReservation(identity, directory, "A", "core-v1"))
    for bootstrap in (False, True):
        with pytest.raises(ValueError):
            async with _common.Direct(bootstrap_config(directory), bootstrap=bootstrap):
                pass
    assert not directory.exists()


def test_init_refuses_a_reserved_directory_before_writing(tmp_path, capsys):
    directory = tmp_path / "isolated"
    identity = ProfileIdentity("profile-a", "installation-a", "lifecycle-a")
    registry = _registry()
    with registry._connection(write=True) as conn:
        registry._reserve(conn, ProfileReservation(identity, directory, "A", "core-v1"))
    assert _init(directory) == 1
    assert "reserved profile" in capsys.readouterr().out
    assert not directory.exists()


def test_a_second_default_is_refused_before_writing(tmp_path, capsys):
    assert _init(tmp_path / "default") == 0
    assert _init(tmp_path / "second") == 1
    assert "already at" in capsys.readouterr().out
    assert not (tmp_path / "second").exists()


@pytest.mark.real_profile_check
async def test_a_corrupt_registry_blocks_direct_access(tmp_path):
    path = Path(PROFILE_REGISTRY_PATH).expanduser()
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_bytes(b"not a database")
    with pytest.raises(ValueError):
        async with _common.Direct(bootstrap_config(tmp_path / "data")):
            pass
    assert not (tmp_path / "data").exists()


async def test_lifespan_checks_its_provider_first(config):
    app = create_app(config)
    inner = getattr(app, "other_asgi_app", app)
    provider = AsyncMock()
    provider.check.side_effect = ValueError("stale")
    inner.state.profile_provider = provider
    with pytest.raises(ValueError, match="stale"):
        async with lifespan(inner):
            pass
    provider.check.assert_awaited_once()
