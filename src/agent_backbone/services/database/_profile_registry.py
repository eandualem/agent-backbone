"""External profile reservations, independent of provider database restoration.

This internal store has no activation operation. Discovery can only describe
non-ready reservations; a successful guard check never authorizes serving work.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unicodedata
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from agent_backbone.services.database._profile_identity_repo import ProfileIdentity


def _resolve(path: Path) -> Path:
    try:
        resolved = path.resolve()
        try:
            mode = resolved.lstat().st_mode
        except FileNotFoundError:
            return resolved
        # Python 3.13's non-strict resolve can leave a symlink loop unresolved.
        if stat.S_ISLNK(mode):
            raise ValueError("Profile path contains an unresolved symbolic link")
        return resolved
    except (OSError, RuntimeError) as exc:
        raise ValueError("Profile path is unavailable") from exc


@dataclass(frozen=True)
class ProfileReservation:
    identity: ProfileIdentity
    data_dir: Path
    display_name: str
    baseline_version: str
    state: str = "creating"

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProfileIdentity):
            raise ValueError("Invalid profile identity")
        if (
            not self.data_dir.is_absolute()
            or ".." in self.data_dir.parts
            or "\x00" in str(self.data_dir)
        ):
            raise ValueError("Profile directory must be normalized and absolute")
        for value in (self.display_name, self.baseline_version):
            if (
                not isinstance(value, str)
                or not value.strip()
                or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
            ):
                raise ValueError("Invalid profile reservation metadata")
        if self.state not in {"creating", "failed"}:
            raise ValueError("Unsupported profile reservation state")

    def summary(self) -> dict:
        return {
            "profile_id": self.identity.profile_id,
            "display_name": self.display_name,
            "kind": "isolated",
            "state": self.state,
        }


class ProfileRegistry:
    """Private control-plane SQLite store at an explicitly selected external path.

    Opening and reading never create or repair it. The caller owns operator
    authorization; this is not a provider API or an agent discovery gateway.
    No environment variables, provider configuration or credentials are read.
    """

    def __init__(self, path: Path):
        self.path = _resolve(path)

    @classmethod
    def create(cls, path: Path) -> ProfileRegistry:
        """Explicitly initialize a new registry, never replace an existing one."""
        registry = cls(path)
        registry.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        registry._check_parent()
        if registry.path.exists():
            registry.list()
            return registry
        fd, temporary = tempfile.mkstemp(prefix=".registry-", dir=registry.path.parent)
        os.close(fd)
        try:
            with cls(Path(temporary))._connection(write=True, initializing=True) as conn:
                conn.execute(
                    "CREATE TABLE profiles ("
                    "profile_id TEXT PRIMARY KEY NOT NULL, "
                    "installation_id TEXT UNIQUE NOT NULL, lifecycle_id TEXT NOT NULL, "
                    "data_dir TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, "
                    "baseline_version TEXT NOT NULL, state TEXT NOT NULL "
                    "CHECK (state IN ('creating', 'failed')))"
                )
                conn.execute("PRAGMA user_version = 1")
            # Publish only a complete database, without replacing a concurrent
            # creator's file. A crash before publication leaves no partial target.
            with suppress(FileExistsError):
                os.link(temporary, registry.path)
            parent = os.open(registry.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
        registry.list()
        return registry

    def _check_parent(self) -> None:
        mode = self.path.parent.stat().st_mode
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise ValueError("Profile registry directory allows untrusted file replacement")

    @contextmanager
    def _connection(self, *, write=False, initializing=False):
        conn = None
        try:
            self._check_parent()
            if not self.path.is_file() or self.path.stat().st_mode & 0o077:
                raise ValueError("Profile registry must be an existing private file")
            mode = "rw" if write else "ro"
            conn = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True)
            conn.row_factory = sqlite3.Row
            if write:
                conn.execute("PRAGMA synchronous = EXTRA")
                conn.execute("PRAGMA fullfsync = ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if not initializing and conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported profile registry schema")
            yield conn
            if write:
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            raise ValueError("Profile registry is unavailable or invalid") from exc
        finally:
            if conn is not None:
                conn.close()

    def _records(self, conn) -> list[ProfileReservation]:
        records = []
        for row in conn.execute("SELECT * FROM profiles ORDER BY profile_id"):
            try:
                record = ProfileReservation(
                    ProfileIdentity(row["profile_id"], row["installation_id"], row["lifecycle_id"]),
                    Path(row["data_dir"]),
                    row["display_name"],
                    row["baseline_version"],
                    row["state"],
                )
            except (IndexError, TypeError) as exc:
                raise ValueError("Invalid profile registry metadata") from exc
            self._outside(record.data_dir)
            for previous in records:
                if (
                    previous.identity.profile_id == record.identity.profile_id
                    or previous.identity.installation_id == record.identity.installation_id
                    or self._overlap(previous.data_dir, record.data_dir)
                ):
                    raise ValueError("Conflicting profile registry metadata")
            records.append(record)
        return records

    def _external(self, data_dir: Path) -> None:
        if not data_dir.is_absolute() or _resolve(data_dir) != data_dir:
            raise ValueError("Profile directory must be canonical and absolute")
        self._outside(data_dir)

    def _outside(self, data_dir: Path) -> None:
        if self._contains(data_dir, self.path):
            raise ValueError("Profile registry must be outside the provider data directory")

    @staticmethod
    def _contains(parent: Path, child: Path) -> bool:
        # Conservatively refuse aliases even before directories exist. Case and
        # Unicode normalization can alias on macOS but not on every filesystem;
        # profile roots must remain distinct on either kind of filesystem.
        def key(path):
            folded = unicodedata.normalize("NFD", str(path)).casefold()
            return Path(unicodedata.normalize("NFD", folded))

        return key(child).is_relative_to(key(parent))

    @classmethod
    def _overlap(cls, first: Path, second: Path) -> bool:
        return cls._contains(first, second) or cls._contains(second, first)

    def reserve(self, reservation: ProfileReservation) -> ProfileReservation:
        """Reserve once, retaining failed tombstones and rejecting conflicting replay."""
        if reservation.state != "creating":
            raise ValueError("New profile reservations must be creating")
        self._external(reservation.data_dir)
        with self._connection(write=True) as conn:
            for stored in self._records(conn):
                if stored.identity.profile_id == reservation.identity.profile_id:
                    if (
                        stored.identity != reservation.identity
                        or stored.data_dir != reservation.data_dir
                        or stored.display_name != reservation.display_name
                        or stored.baseline_version != reservation.baseline_version
                    ):
                        raise ValueError("Profile reservation does not match")
                    if stored.state == "failed":
                        raise ValueError("Profile lifecycle has been invalidated")
                    return stored
                if (
                    stored.identity.installation_id == reservation.identity.installation_id
                    or self._overlap(stored.data_dir, reservation.data_dir)
                ):
                    raise ValueError("Profile reservation conflicts with an existing reservation")
            conn.execute(
                "INSERT INTO profiles VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    reservation.identity.profile_id,
                    reservation.identity.installation_id,
                    reservation.identity.lifecycle_id,
                    str(reservation.data_dir),
                    reservation.display_name,
                    reservation.baseline_version,
                    reservation.state,
                ),
            )
        return reservation

    def list(self) -> dict:
        """Read an atomic snapshot without accessing provider paths or credentials."""
        with self._connection() as conn:
            return {"schema_version": 1, "items": [r.summary() for r in self._records(conn)]}

    def show(self, profile_id: str) -> dict:
        with self._connection() as conn:
            for record in self._records(conn):
                if record.identity.profile_id == profile_id:
                    return {"schema_version": 1, **record.summary(), "binding": None}
        raise ValueError("Profile reservation was not found")

    def check_reservation(self, identity: ProfileIdentity, data_dir: Path) -> ProfileReservation:
        """Compare external identity/path ownership; does not establish readiness.

        Call again for each reconciliation attempt. Do not cache this result as
        serving authority: activation and live service/job fencing are separate.
        """
        self._external(data_dir)
        with self._connection() as conn:
            record = self._matching(conn, identity, data_dir)
            if record.state != "creating":
                raise ValueError("Profile lifecycle has been invalidated")
            return record

    def _matching(self, conn, identity, data_dir):
        for record in self._records(conn):
            if record.identity == identity and record.data_dir == data_dir:
                return record
        raise ValueError("Profile reservation does not match")

    def invalidate(self, identity: ProfileIdentity, data_dir: Path) -> None:
        """Permanently fail this reservation, including across provider DB restore.

        This only fences future reservation checks. It cannot stop a service,
        cancel jobs or replace a generation, and must not be used to claim that.
        """
        with self._connection(write=True) as conn:
            self._matching(conn, identity, data_dir)
            conn.execute(
                "UPDATE profiles SET state = 'failed' WHERE profile_id = ?",
                (identity.profile_id,),
            )
