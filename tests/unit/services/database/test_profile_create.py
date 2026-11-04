"""Creating the empty baseline: reserve once, provision, then publish ready."""

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
        "baseline_version": "empty-v1",
    }


@pytest.fixture
def short_root():
    # Pytest's tmp_path can exceed the Unix socket path limit on macOS.
    with tempfile.TemporaryDirectory(prefix="bb", dir="/tmp") as root:
        yield Path(root).resolve()


@pytest.fixture
def operations(short_root):
    return ProfileOperations(ProfileRegistry.create(short_root / "registry.db"))


def test_create_publishes_a_ready_private_profile_once(operations, monkeypatch):
    ambient = operations.registry.path.parent / "ambient.db"
    monkeypatch.setenv("BACKBONE_DATABASE_URL", f"sqlite+aiosqlite:///{ambient}")
    receipt = operations.apply(request())
    assert (receipt["status"], receipt["error"]) == ("succeeded", None)
    assert operations.apply(request()) == operations.lookup("create-a") == receipt
    assert len(operations.registry.list()["items"]) == 1
    assert not ambient.exists()

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

    monkeypatch.setattr(module, "_provision_empty_profile", interrupted)
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

    monkeypatch.setattr(module, "_provision_empty_profile", fails)
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
