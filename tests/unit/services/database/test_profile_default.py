"""The first profile: a fresh install is the Default profile, in the person's environment."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone.cli import main
from agent_backbone.cli.server import _default_provider
from agent_backbone.config import DEFAULT_DATA_DIR, PROFILE_REGISTRY_PATH
from agent_backbone.services.database import (
    ProfileIdentity,
    ProfileRegistry,
    create_default_profile,
    default_profile_at,
    default_tmux_socket,
)
from agent_backbone.services.database import _profile_default as module


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKBONE_DATA_DIR", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "t"))
    return Path(DEFAULT_DATA_DIR).expanduser()


def registry_path():
    return Path(PROFILE_REGISTRY_PATH).expanduser()


def run(*argv):
    try:
        return main(list(argv)) or 0
    except SystemExit as stopped:
        return stopped.code or 0


def init():
    return run("init")


def serve(root):
    with patch("agent_backbone.services.terminal.pin_tmux_server") as pin:
        provider = asyncio.run(_default_provider(*default_profile_at(root.resolve())))
    return provider, pin


def test_init_creates_the_default_profile_and_up_serves_it(root):
    assert init() == 0
    record = ProfileRegistry(registry_path()).default_reservation()
    assert record.display_name == "Default" and record.data_dir == root.resolve()
    assert record.kind == "legacy_default" and record.state == "ready"
    assert record.binding.credential == root.resolve() / ".env"
    assert record.binding.tmux_socket == default_tmux_socket()
    provider, pin = serve(root)
    assert provider.reservation == record
    assert record.binding.tmux_socket.parent.is_dir()
    pin.assert_called_once_with(record.binding.tmux_socket)


def test_init_again_keeps_the_same_profile(root):
    init()
    first = ProfileRegistry(registry_path()).default_reservation()
    init()
    assert ProfileRegistry(registry_path()).default_reservation() == first


def test_up_refuses_with_no_profile(root):
    with pytest.raises(ValueError, match="backbone init"):
        serve(root)


def test_a_second_directory_cannot_become_default(root, tmp_path, monkeypatch, capsys):
    init()
    other = tmp_path / "other"
    assert run("init", "--data-dir", str(other)) == 1
    assert "already at" in capsys.readouterr().out
    assert ProfileRegistry(registry_path()).default_reservation().data_dir == root.resolve()


def test_init_refuses_a_profile_whose_registry_it_cannot_see(root, capsys):
    # As in an isolated profile's shell, where HOME is the profile's own home.
    init()
    registry_path().unlink()
    env = (root / ".env").read_bytes()
    assert run("init", "--force") == 1
    assert "belongs to another profile" in capsys.readouterr().out
    assert (root / ".env").read_bytes() == env


def test_init_refuses_a_database_it_cannot_read(root, capsys):
    root.mkdir(parents=True)
    (root / "backbone.db").write_bytes(b"not a database")
    assert run("init") == 1
    assert "Cannot read" in capsys.readouterr().out
    assert not (root / ".env").exists()


@pytest.mark.real_profile_check
def test_offline_commands_open_only_the_profiles_own_database(root, tmp_path, monkeypatch):
    from agent_backbone.cli import _common
    from agent_backbone.config import bootstrap_config

    init()
    other = tmp_path / "other.db"
    monkeypatch.setenv("BACKBONE_DATABASE_URL", f"sqlite+aiosqlite:///{other}")

    async def open_direct():
        async with _common.Direct(bootstrap_config()):
            pass

    with pytest.raises(ValueError, match="BACKBONE_DATABASE_URL"):
        asyncio.run(open_direct())
    assert not other.exists()


def test_a_default_made_meanwhile_by_another_init_refuses(root, tmp_path):
    init()
    other = tmp_path / "other"
    other.mkdir()
    (other / "backbone.db").write_bytes((root / "backbone.db").read_bytes())
    with patch.object(ProfileRegistry, "default_reservation", lambda self: None):
        with pytest.raises(ValueError, match="already at"):
            create_default_profile(registry_path(), other, "manual")


def test_attaching_from_another_tmux_server_refuses_clearly(root, monkeypatch):
    from agent_backbone.cli import _common
    from agent_backbone.services.terminal import attach_session, tmux_server_socket

    init()
    _common.pin_default_terminal()
    assert tmux_server_socket() == str(default_tmux_socket())
    monkeypatch.setenv("TMUX", "/tmp/elsewhere/named,1,0")
    with pytest.raises(ValueError, match="different tmux server"):
        attach_session("worker")


def test_a_crash_before_the_identity_is_recorded_is_finished_by_running_init_again(
    root, monkeypatch
):
    def crashes(*args):
        raise ValueError("crash")

    with patch.object(module, "_record_identity", crashes):
        assert init() == 1
    default_profile_at(root.resolve())
    with pytest.raises(ValueError, match="identity"):
        serve(root)
    assert init() == 0
    assert serve(root)[0].reservation.data_dir == root.resolve()


def test_a_database_of_another_installation_refuses(root):
    init()
    record = ProfileRegistry(registry_path()).default_reservation()
    other = ProfileIdentity(record.identity.profile_id, "other", record.identity.lifecycle_id)
    with pytest.raises(ValueError, match="identity changed"):
        module._record_identity(root.resolve() / "backbone.db", other)


async def test_a_copied_database_elsewhere_does_not_start(root, tmp_path):
    await asyncio.to_thread(init)
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "backbone.db").write_bytes((root / "backbone.db").read_bytes())
    with pytest.raises(ValueError):
        default_profile_at(copy.resolve())


def test_create_needs_an_initialized_installation(root):
    root.mkdir(parents=True)
    with pytest.raises(Exception):
        create_default_profile(registry_path(), root, "manual")
    assert ProfileRegistry(registry_path()).default_reservation() is None
