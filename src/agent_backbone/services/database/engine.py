"""Engine construction.

The database is a SQLite file in the data directory and needs no setup.
``BackboneDB`` owns the engine's lifecycle; query logic lives in the
repository modules, which receive connections from it.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.util import asbool


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def is_memory(url: str) -> bool:
    parsed = make_url(url)
    return parsed.database in {None, "", ":memory:", "file::memory:"} or (
        asbool(parsed.query.get("uri", False)) and parsed.query.get("mode") == "memory"
    )


def build_engine(url: str) -> AsyncEngine:
    """Create an async engine; a SQLite file is created owner-only first."""
    if is_sqlite(url) and not is_memory(url):
        parsed = make_url(url)
        db_file = parsed.database
        uri = asbool(parsed.query.get("uri", False))
        if uri and db_file and db_file.startswith("file:"):
            location = urlsplit(db_file)
            db_file = unquote(location.path) if location.netloc in {"", "localhost"} else None
        readonly = uri and (
            parsed.query.get("mode") == "ro" or parsed.query.get("immutable") == "1"
        )
        if db_file and not readonly:
            path = Path(db_file)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            create = 0 if uri and parsed.query.get("mode") in {"ro", "rw"} else os.O_CREAT
            fd = os.open(path, os.O_RDWR | create | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            for suffix in ("-wal", "-shm", "-journal"):
                companion = Path(str(path) + suffix)
                if companion.exists() and not companion.is_symlink():
                    companion.chmod(0o600)
            engine = create_async_engine(url)
            event.listen(engine.sync_engine, "connect", _concurrent_access)
            return engine
    return create_async_engine(url)


def _concurrent_access(dbapi_connection, _record) -> None:
    """Let readers and the writer work at once, and let a writer wait its turn.

    In SQLite's default rollback-journal mode a commit blocks every reader and
    a reader blocks every commit, so a busy installation answered requests
    with "database is locked". Write-ahead logging keeps readers and the
    single writer out of each other's way; the mode is stored in the file.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout = 15000")
        cursor.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        # The switch needs the file to itself for a moment. A connection that
        # cannot get it keeps the file's current mode instead of failing.
        pass
    finally:
        cursor.close()


def redact_url(url: str) -> str:
    """Hide credentials in a database URL for logging.

    Drops the query string too — connection parameters can carry passwords
    or tokens (``?password=…``) that must not reach logs or ``/health``.
    """
    base, _, query = url.partition("?")
    if "@" in base and "://" in base:
        scheme, rest = base.split("://", 1)
        _creds, host = rest.rsplit("@", 1)
        base = f"{scheme}://***@{host}"
    return f"{base}?***" if query else base
