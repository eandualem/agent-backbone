"""Creating the shipped baseline: reserve once, provision, then publish ready."""

import asyncio
import json
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from agent_backbone.cli import main
from agent_backbone.cli._profile_client import resolve_binding, resolve_dotenv_reference
from agent_backbone.config import PROFILE_REGISTRY_PATH
from agent_backbone.services.database import (
    BackboneDB,
    ProfileIdentity,
    ProfileOperations,
    ProfileRegistry,
)
from agent_backbone.services.database import _profile_operations as module


def request(request_id="create-a", display_name="Work"):
    return {
        "schema_version": 1,
        "request_id": request_id,
        "action": "create",
        "display_name": display_name,
        "baseline_version": "core-v1",
    }


@pytest.fixture
def short_root():
    # Pytest's tmp_path can exceed the Unix socket path limit on macOS.
    with tempfile.TemporaryDirectory(prefix="bb", dir="/tmp") as root:
        yield Path(root).resolve()


@pytest.fixture
def operations(short_root):
    return ProfileOperations(ProfileRegistry.create(short_root / "registry.db"))


def test_create_publishes_a_ready_private_profile_once(operations):
    receipt = operations.apply(request())
    assert (receipt["status"], receipt["error"]) == ("succeeded", None)
    assert operations.apply(request()) == operations.lookup("create-a") == receipt
    assert len(operations.registry.list()["items"]) == 1

    record = operations.registry.show(receipt["profile_id"])
    binding = record["binding"]
    assert record["state"] == "ready"
    assert (binding["installation_id"], binding["lifecycle_id"]) == (
        receipt["installation_id"],
        receipt["lifecycle_id"],
    )
    root = Path(binding["credential_reference"]["path"]).parent
    assert root.parent == operations.registry.path.parent / "p"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "home").stat().st_mode) == 0o700
    assert resolve_dotenv_reference(binding["credential_reference"])

    async def stored():
        db = BackboneDB(f"sqlite+aiosqlite:///{root / 'backbone.db'}")
        await db.start()
        try:
            return await db.profile_identity.read(), await db.signing.audience()
        finally:
            await db.stop()

    identity, audience = asyncio.run(stored())
    assert identity == ProfileIdentity(
        receipt["profile_id"], receipt["installation_id"], receipt["lifecycle_id"]
    )
    assert audience == receipt["installation_id"]


def test_two_profiles_share_nothing(operations):
    first = operations.registry.show(operations.apply(request("a"))["profile_id"])["binding"]
    second = operations.registry.show(operations.apply(request("b"))["profile_id"])["binding"]
    for key in ("installation_id", "lifecycle_id", "endpoint", "credential_reference", "tmux"):
        assert first[key] != second[key]
    assert resolve_dotenv_reference(first["credential_reference"]) != resolve_dotenv_reference(
        second["credential_reference"]
    )


def test_interruption_stays_pending_and_never_repeats_effects(operations, monkeypatch):
    calls = []

    async def interrupted(*args):
        calls.append(args)
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_provision_profile", interrupted)
    with pytest.raises(KeyboardInterrupt):
        operations.apply(request())
    receipt = operations.lookup("create-a")
    assert receipt["status"] == "pending"
    assert receipt["profile_id"] and receipt["installation_id"] and receipt["lifecycle_id"]
    assert operations.apply(request()) == receipt
    assert len(calls) == 1
    record = operations.registry.show(receipt["profile_id"])
    assert (record["state"], record["binding"]) == ("creating", None)


def test_failed_provisioning_leaves_a_tombstone(operations, monkeypatch):
    async def fails(*args):
        raise ValueError("private detail")

    monkeypatch.setattr(module, "_provision_profile", fails)
    receipt = operations.apply(request())
    assert receipt["status"] == "failed"
    assert receipt["error"] == {
        "code": "profile_create_failed",
        "message": "Profile could not be created.",
    }
    assert operations.apply(request()) == receipt
    record = operations.registry.show(receipt["profile_id"])
    assert (record["state"], record["binding"]) == ("failed", None)


def test_socket_paths_over_the_limit_are_refused_before_reserving(tmp_path):
    operations = ProfileOperations(ProfileRegistry.create(tmp_path / ("x" * 90) / "registry.db"))
    receipt = operations.apply(request())
    assert receipt["error"]["code"] == "profile_path_too_long"
    assert receipt["profile_id"] is None
    assert operations.registry.list()["items"] == []
    assert not (operations.registry.path.parent / "p").exists()


def _person_git_config(monkeypatch, home, *, email=True):
    home.mkdir(exist_ok=True)
    lines = ["[user]", "\tname = Example Person"] + (
        ["\temail = person@example.com"] if email else []
    )
    (home / ".gitconfig").write_text("\n".join([*lines, "[credential]", "\thelper = store", ""]))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)


def test_git_identity_copy_takes_only_name_and_email(operations, monkeypatch, short_root):
    _person_git_config(monkeypatch, short_root / "person")
    receipt = operations.apply({**request(), "git_identity": "copy"})
    assert receipt["status"] == "succeeded"
    assert operations.registry.show(receipt["profile_id"])["git_identity"] == "copied"
    root = short_root / "p" / receipt["profile_id"]
    copied = (root / "home" / ".gitconfig").read_text()
    assert "Example Person" in copied and "person@example.com" in copied
    assert "helper" not in copied


def test_git_identity_defaults_to_none_and_copies_nothing(operations, monkeypatch, short_root):
    _person_git_config(monkeypatch, short_root / "person")
    for request_id, extra in (("create-a", {}), ("create-b", {"git_identity": "none"})):
        receipt = operations.apply({**request(request_id), **extra})
        assert operations.registry.show(receipt["profile_id"])["git_identity"] == "none"
        assert not (short_root / "p" / receipt["profile_id"] / "home" / ".gitconfig").exists()


def test_git_identity_copy_refuses_without_name_and_email(operations, monkeypatch, short_root):
    _person_git_config(monkeypatch, short_root / "person", email=False)
    receipt = operations.apply({**request(), "git_identity": "copy"})
    assert receipt["error"]["code"] == "git_identity_unavailable"
    assert receipt["profile_id"] is None
    assert operations.registry.list()["items"] == []


@pytest.mark.parametrize("value", ["yes", None])
def test_git_identity_accepts_only_its_values_on_create(value):
    with pytest.raises(ValueError):
        module.validate_profile_operation({**request(), "git_identity": value})
    start = {
        "schema_version": 1,
        "request_id": "start-a",
        "action": "start",
        "profile_id": "profile-a",
        "expected_installation_id": "installation-a",
        "expected_lifecycle_id": "lifecycle-a1",
    }
    with pytest.raises(ValueError):
        module.validate_profile_operation({**start, "git_identity": "none"})


def test_cli_creates_a_profile_the_bound_client_can_resolve(
    short_root, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("HOME", str(short_root))
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request()))
    monkeypatch.setattr(sys, "argv", ["backbone"])
    with pytest.raises(SystemExit) as result:
        main(["profiles", "apply", "--file", str(source), "--json"])
    assert result.value.code == 0
    receipt = json.loads(capsys.readouterr().out)
    assert Path(PROFILE_REGISTRY_PATH).expanduser().is_file()

    binding = resolve_binding(
        (receipt["profile_id"], receipt["installation_id"], receipt["lifecycle_id"])
    )
    assert binding.credential

    with pytest.raises(SystemExit) as result:
        main(["profiles", "show", receipt["profile_id"]])
    assert result.value.code == 0
    output = capsys.readouterr().out
    for label in ("ready", "Endpoint", "Credential file", "Tmux socket"):
        assert label in output


def test_each_profile_keeps_its_own_loopback_port(operations):
    def stored_port(receipt):
        record = operations.registry.show(receipt["profile_id"])
        root = Path(record["binding"]["credential_reference"]["path"]).parent

        async def read():
            db = BackboneDB(f"sqlite+aiosqlite:///{root / 'backbone.db'}")
            await db.start()
            try:
                return (await db.settings.all())["backbone.port"]
            finally:
                await db.stop()

        return asyncio.run(read())

    first, second = (stored_port(operations.apply(request(name))) for name in ("a", "b"))
    assert first != second
    assert 7120 not in {first, second}


def test_a_port_in_use_is_skipped(operations, monkeypatch):
    from agent_backbone.services.database import _profile_registry as registry_module

    busy = registry_module.PROFILE_PORTS[0]
    free = registry_module._port_is_free
    monkeypatch.setattr(registry_module, "_port_is_free", lambda port: port != busy and free(port))
    with operations.registry._connection(write=True) as conn:
        assert operations.registry._allocate_port(conn, "profile-a") != busy


def test_a_new_profile_starts_like_a_fresh_install_and_copies_nothing(operations):
    from agent_backbone.templates import core_starter_content

    receipt = operations.apply(request())
    binding = operations.registry.show(receipt["profile_id"])["binding"]
    root = Path(binding["credential_reference"]["path"]).parent
    starter, files = core_starter_content()
    for relative, content in files.items():
        assert (root / relative).read_text() == content

    async def read():
        db = BackboneDB(f"sqlite+aiosqlite:///{root / 'backbone.db'}")
        await db.start()
        try:
            return await db.settings.all(), await db.agents.list()
        finally:
            await db.stop()

    settings, agents = asyncio.run(read())
    assert settings["agents.shared_policy"] == starter["policies"]
    assert settings["skills.store"] == str(root / "skills")
    assert agents == []
