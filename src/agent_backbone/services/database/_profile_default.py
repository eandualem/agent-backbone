"""The first profile: an installation in the person's own environment, where it is."""

import os
import sqlite3
from pathlib import Path
from secrets import token_hex
from uuid import uuid4

from agent_backbone.config import SQLITE_FILENAME
from agent_backbone.services.database._profile_identity_repo import ProfileIdentity
from agent_backbone.services.database._profile_registry import (
    ProfileBinding,
    ProfileRegistry,
    ProfileReservation,
)

DEFAULT_DISPLAY_NAME = "Default"


def default_tmux_socket() -> Path:
    """The tmux server used when none is selected: the person's own."""
    directory = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return (Path(directory) / f"tmux-{os.geteuid()}" / "default").resolve()


def create_default_profile(
    registry_path: Path, data_dir: Path, baseline: str
) -> ProfileReservation:
    """Record this initialized installation as the Default profile. Safe to repeat.

    The registry commits before the database records the identity: running this again
    finishes a crash between them, while the other order leaves an identity no registry
    explains, which refuses to start.
    """
    root = data_dir.resolve()
    database = root / SQLITE_FILENAME
    registry = ProfileRegistry.create(registry_path)
    reservation = registry.default_reservation()
    if reservation is not None and reservation.data_dir != root:
        raise ValueError(f"The Default profile is already at {reservation.data_dir}")
    if reservation is None:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            installation_id = connection.execute("SELECT audience FROM signing_install").fetchone()
        finally:
            connection.close()
        if installation_id is None:
            raise ValueError("The installation has no identity yet")
        identity = ProfileIdentity(token_hex(6), installation_id[0], str(uuid4()))
        binding = ProfileBinding(root / "run" / "api.sock", root / ".env", default_tmux_socket())
        with registry._connection(write=True) as conn:
            # Another init may have made Default since the read above.
            for record in registry._records(conn):
                if record.kind == "legacy_default":
                    raise ValueError(f"The Default profile is already at {record.data_dir}")
            registry._reserve(
                conn, ProfileReservation(identity, root, DEFAULT_DISPLAY_NAME, baseline)
            )
            conn.execute("INSERT INTO default_profiles VALUES (?)", (identity.profile_id,))
            registry._publish(conn, identity, root, binding, own_tmux=True)
        reservation = registry.default_reservation()
    _record_identity(database, reservation.identity)
    return reservation


def _record_identity(database: Path, identity: ProfileIdentity) -> None:
    connection = sqlite3.connect(database, timeout=30)
    try:
        recorded = connection.execute(
            "SELECT profile_id, installation_id, lifecycle_id FROM profile_identity"
        ).fetchone()
        if recorded == (identity.profile_id, identity.installation_id, identity.lifecycle_id):
            return
        if recorded is not None:
            raise ValueError("The installation's identity changed")
        with connection:
            cursor = connection.execute(
                "INSERT INTO profile_identity"
                " (id, schema_version, profile_id, installation_id, lifecycle_id)"
                " SELECT 1, 1, ?, ?, ? WHERE EXISTS"
                " (SELECT 1 FROM signing_install WHERE id = 1 AND audience = ?)",
                (
                    identity.profile_id,
                    identity.installation_id,
                    identity.lifecycle_id,
                    identity.installation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("The installation's identity changed")
    finally:
        connection.close()
