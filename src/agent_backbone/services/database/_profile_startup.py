"""Nobody runs without a profile: who may open an installation directly."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_backbone.config import PROFILE_REGISTRY_PATH, SQLITE_FILENAME
from agent_backbone.services.database._profile_registry import ProfileRegistry


def default_profile_at(data_dir: Path):
    """The registry and Default profile serving this directory, or a refusal."""
    registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
    reservation = registry.default_reservation()
    if reservation is None or reservation.data_dir != data_dir:
        raise ValueError(f"No profile at {data_dir}: run `backbone init` first")
    return registry, reservation


def check_new_default(data_dir: Path) -> None:
    """Refuse before `init` or setup writes to a directory that cannot become Default."""
    registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
    default = registry.default_reservation()
    if default is None:
        registry.check_unreserved(data_dir)
        # The registry records Default before its database does, so an identity
        # with no registry here belongs to a profile this home cannot see.
        if _has_identity(data_dir / SQLITE_FILENAME):
            raise ValueError(f"{data_dir} belongs to another profile")
    elif default.data_dir != data_dir:
        raise ValueError(f"The Default profile is already at {default.data_dir}")


def _has_identity(database: Path) -> bool:
    if not database.exists():
        return False
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT 1 FROM profile_identity").fetchone() is not None
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise ValueError(f"Cannot read {database}; refusing to initialize it") from None
    except sqlite3.Error:
        raise ValueError(f"Cannot read {database}; refusing to initialize it") from None
    finally:
        connection.close()
