"""Read and record the existing installation for adoption as Default, in place."""

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from agent_backbone.config import DEFAULT_PORT, SQLITE_FILENAME, resolve_data_dir
from agent_backbone.services.database._profile_identity_repo import ProfileIdentity

LEGACY_BASELINE = "legacy"
"""Provenance only: the existing installation was not created from a shipped baseline."""


def default_tmux_socket() -> Path:
    """The tmux server this installation uses when no server is selected."""
    directory = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return (Path(directory) / f"tmux-{os.geteuid()}" / "default").resolve()


def inspect_installation() -> tuple[dict, Path]:
    """The contract's preview, and the Unix endpoint the installation keeps.

    Read-only: never starts, migrates or repairs. Unsafe ownership refuses.
    """
    if os.environ.get("BACKBONE_DATABASE_URL"):
        raise ValueError("Adoption supports the installation's own database only")
    root = resolve_data_dir().resolve()
    database, credential = root / SQLITE_FILENAME, root / ".env"
    info = credential.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("The installation's credential file must be private")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        installation = connection.execute("SELECT audience FROM signing_install").fetchone()
        port = connection.execute(
            "SELECT value FROM settings WHERE key = 'backbone.port'"
        ).fetchone()
        configured = connection.execute(
            "SELECT value FROM settings WHERE key = 'backbone.socket_path'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("The installation's database cannot be read") from exc
    finally:
        connection.close()
    if installation is None:
        raise ValueError("The installation has no identity yet")
    socket = default_tmux_socket()
    # Clients may already use the private socket this installation configured.
    endpoint = Path(json.loads(configured[0])) if configured and json.loads(configured[0]) else None
    if endpoint is None:
        endpoint = root / "run" / "api.sock"
    if not endpoint.is_absolute() or not endpoint.is_relative_to(root):
        raise ValueError("The installation's private socket is outside its data directory")
    fingerprint = {
        "data_dir": str(root),
        "database": str(database),
        "installation_id": installation[0],
        "credential": str(credential),
        "credential_sha256": hashlib.sha256(credential.read_bytes()).hexdigest(),
        "tmux": str(socket),
        "endpoint": str(endpoint),
        "port": port[0] if port else DEFAULT_PORT,
    }
    preview = {
        "schema_version": 1,
        "installation_id": installation[0],
        "data_dir": str(root),
        "tmux": {"socket_path": str(socket)},
        "baseline_version": LEGACY_BASELINE,
        "adoption_token": hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True).encode()
        ).hexdigest(),
    }
    return preview, endpoint


def adoption_preview() -> dict:
    return inspect_installation()[0]


def back_up_database(database: Path, target: Path) -> None:
    source = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()


def record_identity(database: Path, identity: ProfileIdentity) -> None:
    connection = sqlite3.connect(database, timeout=30)
    try:
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
