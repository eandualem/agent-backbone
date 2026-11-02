"""External reservations survive provider restore without advertising readiness."""

import json
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_backbone.config import sqlite_url
from agent_backbone.services.database import (
    BackboneDB,
    ProfileIdentity,
    ProfileRegistry,
    ProfileReservation,
)


@pytest.fixture
def reservation(tmp_path):
    return ProfileReservation(
        ProfileIdentity("profile-a", "installation-a", "lifecycle-a1"),
        (tmp_path / "providers" / "a").resolve(),
        "Work",
        "baseline-example-v1",
    )


@pytest.fixture
def registry(tmp_path):
    return ProfileRegistry.create(tmp_path / "control" / "registry.db")


def test_read_never_creates_or_repairs_registry(tmp_path):
    path = tmp_path / "absent" / "registry.db"
    registry = ProfileRegistry(path)
    with pytest.raises(ValueError):
        registry.list()
    assert not path.parent.exists()
    path.parent.mkdir(mode=0o700)
    path.write_bytes(b"")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="schema"):
        ProfileRegistry.create(path)
    assert path.read_bytes() == b""


def test_empty_and_nonready_discovery_are_read_only(registry, reservation):
    assert registry.list() == {"schema_version": 1, "items": []}
    assert registry.reserve(reservation) == reservation
    before = registry.path.read_bytes(), registry.path.stat().st_mtime_ns
    fixture = json.loads(
        (Path(__file__).parents[4] / "docs/contracts/profiles-v1.json").read_text()
    )
    assert registry.show("profile-a") == fixture["discovery"]["show_creating"]
    expected = dict(fixture["discovery"]["list"])
    expected["items"] = [reservation.summary()]
    assert registry.list() == expected
    assert ProfileRegistry.create(registry.path).show("profile-a") == registry.show("profile-a")
    assert before == (registry.path.read_bytes(), registry.path.stat().st_mtime_ns)
    assert not reservation.data_dir.exists()
    assert registry.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="not found"):
        registry.show("unknown")


def test_ordinary_reopen_and_exact_replay_preserve_identity(registry, reservation):
    registry.reserve(reservation)
    reopened = ProfileRegistry(registry.path)
    assert reopened.reserve(reservation) == reservation
    assert reopened.check_reservation(reservation.identity, reservation.data_dir) == reservation


@pytest.mark.parametrize(
    "changes",
    [
        {"identity": ProfileIdentity("profile-a", "installation-other", "lifecycle-a1")},
        {"identity": ProfileIdentity("profile-a", "installation-a", "lifecycle-a2")},
        {"display_name": "Changed"},
        {"baseline_version": "other"},
    ],
)
def test_conflicting_replay_never_overwrites(registry, reservation, changes):
    registry.reserve(reservation)
    with pytest.raises(ValueError, match="does not match"):
        registry.reserve(replace(reservation, **changes))
    assert registry.check_reservation(reservation.identity, reservation.data_dir) == reservation


def test_copied_identity_and_overlapping_paths_are_rejected(registry, reservation):
    registry.reserve(reservation)
    other = replace(
        reservation,
        identity=ProfileIdentity("profile-b", "installation-b", "lifecycle-b1"),
        data_dir=reservation.data_dir.with_name("b"),
    )
    conflicts = [
        replace(other, identity=replace(other.identity, installation_id="installation-a")),
        replace(other, data_dir=reservation.data_dir),
        replace(other, data_dir=reservation.data_dir / "child"),
        replace(other, data_dir=reservation.data_dir.parent),
        replace(other, data_dir=reservation.data_dir.with_name("A")),
    ]
    for conflict in conflicts:
        with pytest.raises(ValueError, match="conflicts"):
            registry.reserve(conflict)
    registry.reserve(other)
    assert len(registry.list()["items"]) == 2


def test_registry_cannot_be_restored_as_part_of_provider_directory(registry, reservation):
    with pytest.raises(ValueError, match="outside"):
        registry.reserve(replace(reservation, data_dir=registry.path.parent))
    with pytest.raises(ValueError, match="outside"):
        registry.reserve(replace(reservation, data_dir=registry.path.parent.with_name("CONTROL")))
    assert registry.list()["items"] == []


@pytest.mark.parametrize("names", [("caf\u00e9", "cafe\u0301"), ("\u0390", "\u03aa\u0301")])
def test_unicode_directory_aliases_cannot_acquire_separate_reservations(
    registry, reservation, names
):
    registry.reserve(replace(reservation, data_dir=reservation.data_dir.with_name(names[0])))
    with pytest.raises(ValueError, match="conflicts"):
        registry.reserve(
            replace(
                reservation,
                identity=ProfileIdentity("profile-b", "installation-b", "lifecycle-b1"),
                data_dir=reservation.data_dir.with_name(names[1]),
            )
        )


def test_symlink_alias_cannot_reserve_another_profiles_directory(registry, reservation, tmp_path):
    registry.reserve(reservation)
    alias = tmp_path / "alias"
    alias.symlink_to(reservation.data_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        registry.reserve(replace(reservation, data_dir=alias))
    with pytest.raises(ValueError, match="conflicts"):
        registry.reserve(
            replace(
                reservation,
                identity=ProfileIdentity("profile-b", "installation-b", "lifecycle-b1"),
                data_dir=alias.resolve(),
            )
        )

    pending = replace(
        reservation,
        identity=ProfileIdentity("profile-b", "installation-b", "lifecycle-b1"),
        data_dir=tmp_path / "pending",
    )
    pending.data_dir.symlink_to(reservation.data_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        registry.reserve(pending)
    assert len(registry.list()["items"]) == 1


@pytest.mark.parametrize("same_input", [True, False])
def test_concurrent_reservations_serialize_before_reading(registry, reservation, same_input):
    other = reservation if same_input else replace(reservation, display_name="Other")

    def reserve(value):
        try:
            return ProfileRegistry(registry.path).reserve(value)
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [reservation, other]))
    assert sum(isinstance(r, ValueError) for r in results) == (0 if same_input else 1)
    stored = registry.check_reservation(reservation.identity, reservation.data_dir)
    assert all(result == stored for result in results if not isinstance(result, ValueError))


def test_invalidation_requires_exact_identity_and_path(registry, reservation):
    registry.reserve(reservation)
    for identity, directory in [
        (replace(reservation.identity, lifecycle_id="stale"), reservation.data_dir),
        (reservation.identity, reservation.data_dir.with_name("b")),
    ]:
        with pytest.raises(ValueError, match="does not match"):
            registry.invalidate(identity, directory)
        with pytest.raises(ValueError, match="does not match"):
            registry.check_reservation(identity, directory)
    assert registry.check_reservation(reservation.identity, reservation.data_dir) == reservation


async def test_provider_database_restore_cannot_resurrect_invalidated_reservation(
    registry, reservation, tmp_path
):
    directory = reservation.data_dir
    async with BackboneDB.connect(sqlite_url(directory)) as db:
        identity = replace(reservation.identity, installation_id=await db.signing.audience())
        await db.profile_identity.reserve(identity)
    reservation = replace(reservation, identity=identity)
    registry.reserve(reservation)
    backup = tmp_path / "backup"
    shutil.copytree(directory, backup)
    registry.invalidate(identity, directory)
    registry.invalidate(identity, directory)
    shutil.rmtree(directory)
    shutil.copytree(backup, directory)
    async with BackboneDB.connect(sqlite_url(directory)) as db:
        assert await db.profile_identity.read() == identity
        with pytest.raises(ValueError, match="invalidated"):
            registry.check_reservation(await db.profile_identity.read(), directory)
    with pytest.raises(ValueError, match="invalidated"):
        ProfileRegistry(registry.path).reserve(reservation)
    assert registry.show(identity.profile_id)["binding"] is None
    assert registry.list()["items"][0]["state"] == "failed"
    with pytest.raises(ValueError, match="does not match"):
        registry.reserve(replace(reservation, identity=replace(identity, lifecycle_id="new")))


@pytest.mark.parametrize(
    "statement",
    [
        "PRAGMA user_version = 2",
        "UPDATE profiles SET profile_id = ''",
        "UPDATE profiles SET data_dir = 'relative'",
        "UPDATE profiles SET baseline_version = ''",
        "DROP TABLE profiles",
        "UPDATE profiles SET data_dir = X'2F61'",
        "ALTER TABLE profiles RENAME COLUMN display_name TO name",
        "UPDATE profiles SET data_dir = '/nul' || char(0) || 'x'",
    ],
)
def test_invalid_metadata_fails_closed_without_repair(registry, reservation, statement):
    registry.reserve(reservation)
    with sqlite3.connect(registry.path) as conn:
        conn.execute(statement)
    before = registry.path.read_bytes()
    for operation in [
        registry.list,
        lambda: registry.show("profile-a"),
        lambda: registry.reserve(reservation),
        lambda: registry.check_reservation(reservation.identity, reservation.data_dir),
        lambda: registry.invalidate(reservation.identity, reservation.data_dir),
    ]:
        with pytest.raises(ValueError):
            operation()
    assert registry.path.read_bytes() == before


def test_ready_is_not_a_reservation_state(reservation):
    with pytest.raises(ValueError, match="state"):
        replace(reservation, state="ready")


def test_changed_provider_path_cannot_block_discovery_or_invalidation(registry, reservation):
    registry.reserve(reservation)
    other = replace(
        reservation,
        identity=ProfileIdentity("profile-b", "installation-b", "lifecycle-b1"),
        data_dir=reservation.data_dir.with_name("b"),
    )
    registry.reserve(other)
    reservation.data_dir.parent.mkdir()
    reservation.data_dir.symlink_to(other.data_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        registry.check_reservation(reservation.identity, reservation.data_dir)
    assert registry.check_reservation(other.identity, other.data_dir) == other
    assert len(registry.list()["items"]) == 2
    registry.invalidate(reservation.identity, reservation.data_dir)
    registry.invalidate(other.identity, other.data_dir)
    assert registry.show("profile-a")["state"] == "failed"
    reservation.data_dir.unlink()
    with pytest.raises(ValueError, match="invalidated"):
        registry.check_reservation(reservation.identity, reservation.data_dir)
    with pytest.raises(ValueError, match="invalidated"):
        registry.check_reservation(other.identity, other.data_dir)


def test_interrupted_initialization_never_publishes_an_empty_registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.db"
    original = ProfileRegistry._connection

    @contextmanager
    def interrupt(self, **kwargs):
        with original(self, **kwargs) as conn:
            yield conn
            raise RuntimeError("interrupted before commit")

    with monkeypatch.context() as patch:
        patch.setattr(ProfileRegistry, "_connection", interrupt)
        with pytest.raises(RuntimeError, match="interrupted"):
            ProfileRegistry.create(path)
    assert not path.exists()
    assert ProfileRegistry.create(path).list()["items"] == []


def test_concurrent_initialization_publishes_only_complete_registries(tmp_path):
    path = tmp_path / "registry.db"
    with ThreadPoolExecutor(max_workers=2) as pool:
        registries = list(pool.map(ProfileRegistry.create, [path, path]))
    assert all(registry.list() == {"schema_version": 1, "items": []} for registry in registries)


def test_symlink_loops_refuse_reconciliation_without_blocking_invalidation(registry, reservation):
    registry.reserve(reservation)
    reservation.data_dir.parent.mkdir()
    reservation.data_dir.symlink_to(reservation.data_dir)
    with pytest.raises(ValueError, match="Profile path"):
        registry.check_reservation(reservation.identity, reservation.data_dir)
    with pytest.raises(ValueError, match="Profile path"):
        registry.reserve(reservation)
    with pytest.raises(ValueError, match="Profile path"):
        ProfileRegistry(reservation.data_dir)
    with pytest.raises(ValueError, match="Profile path"):
        ProfileRegistry(reservation.data_dir / "registry.db")
    assert registry.show("profile-a")["state"] == "creating"
    registry.invalidate(reservation.identity, reservation.data_dir)
    reservation.data_dir.unlink()
    with pytest.raises(ValueError, match="invalidated"):
        registry.check_reservation(reservation.identity, reservation.data_dir)


@pytest.mark.parametrize("mode", [0o777, 0o1777])
def test_registry_refuses_a_replaceable_parent_directory(tmp_path, mode):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(mode)
    path = parent / "registry.db"
    with pytest.raises(ValueError, match="directory"):
        ProfileRegistry.create(path)
    assert not path.exists()
    parent.chmod(0o700)
    registry = ProfileRegistry.create(path)
    parent.chmod(mode)
    with pytest.raises(ValueError, match="directory"):
        registry.list()
    with pytest.raises(ValueError, match="directory"):
        ProfileRegistry.create(path)
    parent.chmod(0o700)
    assert registry.list()["items"] == []


def test_registry_requires_its_parent_to_belong_to_the_current_user(registry, monkeypatch):
    owner = registry.path.parent.stat().st_uid
    monkeypatch.setattr(
        "agent_backbone.services.database._profile_registry.os.geteuid", lambda: owner + 1
    )
    with pytest.raises(ValueError, match="directory"):
        registry.list()
    with pytest.raises(ValueError, match="directory"):
        ProfileRegistry.create(registry.path)


def test_interrupted_invalidation_requires_explicit_replay_for_journal_recovery(
    registry, reservation
):
    registry.reserve(reservation)
    # Spill enough pages to leave a hot journal when the child exits without
    # committing. No service, signals to other processes or model call is used.
    with sqlite3.connect(registry.path) as conn:
        conn.execute("CREATE TABLE spill (data BLOB)")
        conn.execute("INSERT INTO spill VALUES (zeroblob(262144))")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sqlite3, sys; "
            "c = sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA cache_size=1'); "
            "c.execute('BEGIN IMMEDIATE'); "
            "c.execute(\"UPDATE profiles SET state = 'failed'\"); "
            "c.execute('UPDATE spill SET data=randomblob(262144)'); "
            "os._exit(0)",
            str(registry.path),
        ],
        check=True,
        timeout=10,
    )
    journal = Path(str(registry.path) + "-journal")
    assert journal.exists()
    before = registry.path.read_bytes(), journal.read_bytes()
    for read in (registry.list, lambda: registry.show(reservation.identity.profile_id)):
        with pytest.raises(ValueError, match="unavailable") as error:
            read()
        assert error.value.__cause__.sqlite_errorname == "SQLITE_READONLY_ROLLBACK"
        assert (registry.path.read_bytes(), journal.read_bytes()) == before
    registry.invalidate(reservation.identity, reservation.data_dir)
    assert registry.show(reservation.identity.profile_id)["state"] == "failed"
    assert not journal.exists()
