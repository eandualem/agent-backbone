"""External reservations survive provider restore without advertising readiness."""

import hashlib
import json
import os
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
    ProfileBinding,
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


def test_operation_fence_blocks_invalidation_across_processes(registry, reservation):
    registry.reserve(reservation)
    script = """
import sys
from pathlib import Path
from agent_backbone.services.database import ProfileRegistry, ProfileIdentity
registry = ProfileRegistry(Path(sys.argv[1]))
identity = ProfileIdentity(*sys.argv[3:])
try:
    registry.invalidate(identity, Path(sys.argv[2]))
except ValueError as exc:
    assert str(exc) == 'Profile operation fence is busy'
    sys.exit(7)
"""
    command = [
        sys.executable,
        "-c",
        script,
        str(registry.path),
        str(reservation.data_dir),
        *vars(reservation.identity).values(),
    ]
    with registry.operation(reservation.identity, reservation.data_dir):
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert result.returncode == 7, result.stderr
        assert registry.check_reservation(reservation.identity, reservation.data_dir) == reservation
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    with pytest.raises(ValueError, match="invalidated"):
        with registry.operation(reservation.identity, reservation.data_dir):
            pytest.fail("invalidated operation admitted")
    registry.invalidate(reservation.identity, reservation.data_dir)  # Exact replay remains valid.


def test_operation_fence_is_per_profile_and_releases_on_error(registry, reservation):
    registry.reserve(reservation)
    other = replace(
        reservation,
        identity=ProfileIdentity("profile-b", "installation-b", "lifecycle-b1"),
        data_dir=reservation.data_dir.with_name("b"),
    )
    registry.reserve(other)
    with pytest.raises(RuntimeError, match="operation failed"):
        with registry.operation(reservation.identity, reservation.data_dir):
            registry.invalidate(other.identity, other.data_dir)
            with registry.operation(reservation.identity, reservation.data_dir):
                with pytest.raises(ValueError, match="fence is busy"):
                    registry.invalidate(reservation.identity, reservation.data_dir)
            raise RuntimeError("operation failed")
    registry.invalidate(reservation.identity, reservation.data_dir)


def test_operation_directory_cannot_be_provider_or_project_storage(registry, reservation):
    directory = registry.path.with_name(registry.path.name + ".operations")
    for path in (directory, directory / "child", directory.with_name(directory.name.upper())):
        with pytest.raises(ValueError, match="outside"):
            registry.reserve(replace(reservation, data_dir=path))
    registry.reserve(reservation)
    directory.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="profile storage"):
        registry.claim_projects(reservation.identity, reservation.data_dir, (directory,))
    # A registry written before fences existed may already claim this location.
    with sqlite3.connect(registry.path) as conn:
        conn.execute(
            "INSERT INTO project_claims VALUES (?, ?, ?, ?)",
            (str(directory), *vars(reservation.identity).values()),
        )
    with pytest.raises(ValueError, match="project storage"):
        with registry.operation(reservation.identity, reservation.data_dir):
            pytest.fail("conflicting storage admitted")
    assert not list(directory.iterdir())


@pytest.mark.parametrize("suffix", ["", "/child"])
def test_old_fence_directory_reservations_remain_readable(registry, reservation, suffix):
    registry.reserve(reservation)
    directory = registry.path.with_name(registry.path.name + ".operations")
    legacy = replace(
        reservation,
        identity=ProfileIdentity("profile-old", "installation-old", "lifecycle-old"),
        data_dir=Path(str(directory) + suffix),
    )
    # Simulate a valid reservation written before the fence directory was reserved.
    with sqlite3.connect(registry.path) as conn:
        conn.execute(
            "INSERT INTO profiles VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                *vars(legacy.identity).values(),
                str(legacy.data_dir),
                legacy.display_name,
                legacy.baseline_version,
                legacy.state,
            ),
        )
    before = registry.path.read_bytes()
    assert len(registry.list()["items"]) == 2
    assert registry.show(legacy.identity.profile_id)["binding"] is None
    assert registry.check_reservation(legacy.identity, legacy.data_dir) == legacy
    assert registry.check_reservation(reservation.identity, reservation.data_dir) == reservation
    for item in (legacy, reservation):
        with pytest.raises(ValueError, match="provider storage"):
            with registry.operation(item.identity, item.data_dir):
                pytest.fail("conflicting storage admitted")
        with pytest.raises(ValueError, match="provider storage"):
            registry.invalidate(item.identity, item.data_dir)
    assert registry.path.read_bytes() == before
    assert not directory.exists()


@pytest.mark.parametrize("suffix", ["", "/child"])
def test_unreserved_provider_cannot_start_in_fence_directory(registry, reservation, suffix):
    registry.reserve(reservation)
    directory = registry.path.with_name(registry.path.name + ".operations")
    with registry.operation(reservation.identity, reservation.data_dir):
        with pytest.raises(ValueError, match="fence must be outside"):
            registry.check_unadopted(Path(str(directory) + suffix))
    registry.check_unadopted(reservation.data_dir.with_name("legacy"))


def test_concurrent_invalidation_reports_fence_contention(registry, reservation):
    registry.reserve(reservation)
    with registry._operation_fence(reservation.identity, reservation.data_dir, exclusive=True):
        with pytest.raises(ValueError, match="fence is busy"):
            registry.invalidate(reservation.identity, reservation.data_dir)
    registry.invalidate(reservation.identity, reservation.data_dir)
    registry.invalidate(reservation.identity, reservation.data_dir)


def test_registry_hardlink_cannot_bypass_an_active_fence(registry, reservation):
    registry.reserve(reservation)
    alias = registry.path.with_name("alias.db")
    with registry.operation(reservation.identity, reservation.data_dir):
        os.link(registry.path, alias)
        try:
            with pytest.raises(ValueError, match="unaliased registry"):
                ProfileRegistry(alias).invalidate(reservation.identity, reservation.data_dir)
        finally:
            alias.unlink()
    registry.invalidate(reservation.identity, reservation.data_dir)


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "fifo", "public"])
def test_operation_fence_refuses_unsafe_lock_files(registry, reservation, tmp_path, alias):
    registry.reserve(reservation)
    with registry.operation(reservation.identity, reservation.data_dir):
        pass
    directory = registry.path.with_name(registry.path.name + ".operations")
    lock = directory / hashlib.sha256(reservation.identity.profile_id.encode()).hexdigest()
    target = tmp_path / "unrelated"
    target.write_text("sentinel")
    lock.unlink()
    if alias == "symlink":
        lock.symlink_to(target)
    elif alias == "hardlink":
        os.link(target, lock)
    elif alias == "fifo":
        os.mkfifo(lock)
    else:
        lock.touch()
        lock.chmod(0o644)
    with pytest.raises(ValueError, match="fence"):
        registry.invalidate(reservation.identity, reservation.data_dir)
    assert registry.check_reservation(reservation.identity, reservation.data_dir) == reservation
    assert target.read_text() == "sentinel"


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
        "PRAGMA user_version = 5",
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


def test_ready_requires_a_connection_binding(reservation):
    with pytest.raises(ValueError, match="binding"):
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


def _ready_binding(root: Path) -> ProfileBinding:
    return ProfileBinding(root / "run" / "api.sock", root / ".env", root / "tmux" / "server.sock")


def _published(tmp_path, reservation):
    registry = ProfileRegistry.create(tmp_path / "registry" / "registry.db")
    registry.reserve(reservation)
    binding = _ready_binding(reservation.data_dir)
    return (
        registry,
        binding,
        registry.publish_ready(reservation.identity, reservation.data_dir, binding),
    )


def test_publish_ready_emits_the_agreed_discovery_shape(tmp_path, reservation):
    registry, binding, published = _published(tmp_path, reservation)
    root = reservation.data_dir
    assert published.state == "ready"
    assert registry.list()["items"][0]["state"] == "ready"
    assert registry.show("profile-a") == {
        "schema_version": 1,
        "profile_id": "profile-a",
        "display_name": reservation.display_name,
        "kind": "isolated",
        "state": "ready",
        "binding": {
            "installation_id": "installation-a",
            "lifecycle_id": "lifecycle-a1",
            "endpoint": {"kind": "unix", "path": str(root / "run" / "api.sock")},
            "credential_reference": {
                "kind": "dotenv",
                "path": str(root / ".env"),
                "key": "BACKBONE_API_KEY",
            },
            "tmux": {"socket_path": str(root / "tmux" / "server.sock")},
        },
    }
    assert registry.check_reservation(reservation.identity, root) == published
    assert registry.publish_ready(reservation.identity, root, binding) == published


def test_ready_shape_matches_the_shared_fixture_keys(tmp_path, reservation):
    fixture = json.loads(Path("docs/contracts/profiles-v1.json").read_text())["discovery"]
    registry, _, _ = _published(tmp_path, reservation)
    shown = registry.show("profile-a")
    expected = fixture["show_ready"]
    assert set(shown) == set(expected)
    assert set(shown["binding"]) == set(expected["binding"])
    for key in ("endpoint", "credential_reference", "tmux"):
        assert set(shown["binding"][key]) == set(expected["binding"][key])


def test_ready_refuses_changed_binding_and_paths_outside_the_profile(tmp_path, reservation):
    registry, binding, _ = _published(tmp_path, reservation)
    root = reservation.data_dir
    with pytest.raises(ValueError, match="does not match"):
        registry.publish_ready(
            reservation.identity, root, replace(binding, endpoint=root / "run" / "other.sock")
        )
    with pytest.raises(ValueError, match="outside its reserved directory"):
        registry.publish_ready(
            reservation.identity, root, replace(binding, endpoint=tmp_path / "outside.sock")
        )


def test_invalidated_profile_never_returns_to_ready(tmp_path, reservation):
    registry, binding, _ = _published(tmp_path, reservation)
    root = reservation.data_dir
    registry.invalidate(reservation.identity, root)
    assert registry.show("profile-a")["state"] == "failed"
    assert registry.show("profile-a")["binding"] is None
    with pytest.raises(ValueError, match="invalidated"):
        registry.publish_ready(reservation.identity, root, binding)
    with pytest.raises(ValueError, match="invalidated"):
        registry.check_reservation(reservation.identity, root)


def test_ready_still_refuses_legacy_startup_in_its_directory(tmp_path, reservation):
    registry, _, _ = _published(tmp_path, reservation)
    with pytest.raises(ValueError, match="reserved profile"):
        registry.check_unadopted(reservation.data_dir)


def test_discovery_reads_an_older_registry_without_upgrading_it(tmp_path, reservation):
    path = tmp_path / "registry" / "registry.db"
    ProfileRegistry.create(path).reserve(reservation)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE profile_bindings")
        conn.execute("PRAGMA user_version = 3")
    registry = ProfileRegistry(path)
    assert registry.show("profile-a")["state"] == "creating"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    with pytest.raises(ValueError, match="initialization"):
        registry.publish_ready(
            reservation.identity, reservation.data_dir, _ready_binding(reservation.data_dir)
        )
    ProfileRegistry.create(path)
    assert (
        registry.publish_ready(
            reservation.identity, reservation.data_dir, _ready_binding(reservation.data_dir)
        ).state
        == "ready"
    )
