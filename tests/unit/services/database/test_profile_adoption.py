"""Adopting the existing installation records it in place and keeps it working."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_backbone.config import PROFILE_REGISTRY_PATH, sqlite_url
from agent_backbone.services.database import (
    BackboneDB,
    ProfileIdentity,
    ProfileOperations,
    ProfileRegistry,
    adoption_preview,
    check_profile_startup,
    default_tmux_socket,
    reconcile_profile_provider,
)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = (tmp_path / "default").resolve()
    root.mkdir(mode=0o700)
    (root / ".env").write_text("BACKBONE_API_KEY=operator-key\n")
    (root / ".env").chmod(0o600)
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(root))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "t"))

    async def prepare():
        async with BackboneDB.connect(sqlite_url(root)) as db:
            await db.agents.upsert(
                "worker",
                dir=str(root),
                runtime="codex",
                model=None,
                repo="",
                tags=[],
                env={},
                description="",
                unattended=False,
            )
            return await db.signing.audience()

    return root, asyncio.run(prepare())


def operations():
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    return ProfileOperations(registry, services=MagicMock())


def request(preview, request_id="adopt-1"):
    return {
        "schema_version": 1,
        "request_id": request_id,
        "action": "adopt_default",
        "expected_installation_id": preview["installation_id"],
        "adoption_token": preview["adoption_token"],
    }


def test_preview_reads_without_changing_anything(installation):
    root, audience = installation
    before = (root / "backbone.db").read_bytes()
    preview = adoption_preview()
    assert preview["installation_id"] == audience
    assert preview["data_dir"] == str(root)
    assert preview["tmux"]["socket_path"] == str(default_tmux_socket())
    assert adoption_preview() == preview
    assert (root / "backbone.db").read_bytes() == before
    assert not Path(PROFILE_REGISTRY_PATH).expanduser().exists()


def test_preview_token_changes_when_the_credential_changes(installation):
    root, _ = installation
    first = adoption_preview()["adoption_token"]
    (root / ".env").write_text("BACKBONE_API_KEY=rotated\n")
    assert adoption_preview()["adoption_token"] != first


def test_preview_refuses_an_external_database(installation, monkeypatch):
    monkeypatch.setenv("BACKBONE_DATABASE_URL", "sqlite+aiosqlite:////elsewhere.db")
    with pytest.raises(ValueError, match="own database"):
        adoption_preview()


def test_adoption_records_in_place_and_keeps_data_and_credentials(installation):
    root, audience = installation
    credential = (root / ".env").read_bytes()
    ops = operations()
    receipt = ops.apply(request(adoption_preview()))
    assert (receipt["status"], receipt["error"]) == ("succeeded", None)
    assert receipt["installation_id"] == audience
    record = ops.registry.show(receipt["profile_id"])
    assert (record["kind"], record["state"], record["display_name"]) == (
        "legacy_default",
        "ready",
        "Default",
    )
    assert record["binding"]["tmux"]["socket_path"] == str(default_tmux_socket())
    assert (root / ".env").read_bytes() == credential
    backup = ops.registry.path.parent / "backups" / f"{receipt['profile_id']}.db"
    assert os.stat(backup).st_mode & 0o077 == 0

    async def stored():
        async with BackboneDB.connect(sqlite_url(root)) as db:
            return await db.profile_identity.read(), [a["name"] for a in await db.agents.list()]

    identity, agents = asyncio.run(stored())
    assert identity == ProfileIdentity(receipt["profile_id"], audience, receipt["lifecycle_id"])
    assert agents == ["worker"]


def test_adoption_keeps_a_configured_private_socket(installation):
    root, _ = installation
    configured = root / "run" / "backbone.sock"

    async def configure():
        async with BackboneDB.connect(sqlite_url(root)) as db:
            await db.settings.set("backbone.socket_path", str(configured))

    asyncio.run(configure())
    ops = operations()
    receipt = ops.apply(request(adoption_preview()))
    binding = ops.registry.show(receipt["profile_id"])["binding"]
    assert binding["endpoint"]["path"] == str(configured)


def test_a_new_request_returns_the_same_tuple_and_replay_is_unchanged(installation):
    ops = operations()
    first = ops.apply(request(adoption_preview()))
    assert ops.apply(request(adoption_preview())) == first
    second = ops.apply(request(adoption_preview(), "adopt-2"))
    assert (second["profile_id"], second["lifecycle_id"]) == (
        first["profile_id"],
        first["lifecycle_id"],
    )
    assert second["request_id"] == "adopt-2"
    assert len(ops.registry.list()["items"]) == 1


def test_a_changed_installation_is_refused_and_nothing_is_recorded(installation):
    root, _ = installation
    preview = adoption_preview()
    (root / ".env").write_text("BACKBONE_API_KEY=rotated\n")
    ops = operations()
    assert ops.apply(request(preview))["error"]["code"] == "adoption_changed"
    assert ops.registry.list()["items"] == []


def test_failure_leaves_the_installation_startable(installation, monkeypatch):
    from agent_backbone.services.database import _profile_operations as module

    root, _ = installation

    def fails(*args):
        raise ValueError("private detail")

    monkeypatch.setattr(module, "record_identity", fails)
    ops = operations()
    assert ops.apply(request(adoption_preview()))["error"]["code"] == "adoption_unavailable"
    assert ops.registry.list()["items"] == []
    asyncio.run(check_profile_startup(sqlite_url(root), root))


def test_adopted_installation_starts_and_a_copy_elsewhere_does_not(installation, tmp_path):
    root, _ = installation
    ops = operations()
    receipt = ops.apply(request(adoption_preview()))
    asyncio.run(check_profile_startup(sqlite_url(root), root))
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "backbone.db").write_bytes((root / "backbone.db").read_bytes())
    with pytest.raises(ValueError, match="not ready"):
        asyncio.run(check_profile_startup(sqlite_url(copy), copy))
    reservation = ops.registry.adopted_reservation(root)
    provider = asyncio.run(
        reconcile_profile_provider(
            ops.registry, reservation, root / "backbone.db", default_tmux_socket()
        )
    )
    assert provider.identity_response()["profile_id"] == receipt["profile_id"]


def test_without_services_adoption_stays_refused(installation):
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    result = ProfileOperations(registry).apply(request(adoption_preview()))
    assert result["error"]["code"] == "adoption_unavailable"


def test_preview_cli_json(installation, capsys):
    from agent_backbone.cli import main

    with pytest.raises(SystemExit) as result:
        main(["profiles", "adoption-preview", "--json"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == adoption_preview()
