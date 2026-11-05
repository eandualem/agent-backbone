"""Reservation preflight before legacy initialization, migrations or launch."""

import asyncio
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.util import asbool

from agent_backbone.config import PROFILE_REGISTRY_PATH
from agent_backbone.services.database._profile_registry import ProfileRegistry
from agent_backbone.services.database.engine import is_memory


async def check_profile_startup(database_url: str, data_dir: Path, *, recover: bool = True) -> None:
    """Refuse reservations before mkdir, migrations or direct-mode agent starts.

    Directory ownership catches pre-identity restores; any persisted identity catches
    copied databases outside that tree. This is a refusal guard, not identity attestation
    or running-service fencing.
    """
    registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
    default = await asyncio.to_thread(registry.default_reservation)
    if default is not None and default.data_dir == data_dir.resolve():
        return  # `up` reconciles its identity before serving.
    await asyncio.to_thread(registry.check_unadopted, data_dir.resolve())
    url = make_url(database_url)
    can_recover = False
    if url.get_backend_name() == "sqlite":
        if is_memory(database_url):
            return  # No persisted identity exists in a new in-memory database.
        location = url.database
        if asbool(url.query.get("uri", False)) and location.startswith("file:"):
            parsed = urlsplit(location)
            if parsed.netloc not in {"", "localhost"}:
                raise ValueError("Provider database location is unsupported")
            location = unquote(parsed.path)
        path = Path(location).resolve()
        await asyncio.to_thread(registry.check_unadopted, path)
        try:
            path.stat()
        except FileNotFoundError:
            return  # Ordinary unadopted initialization may create this file.
        can_recover = recover and not (
            asbool(url.query.get("uri", False))
            and (url.query.get("mode") == "ro" or url.query.get("immutable") == "1")
        )
        # Start read-only; only SQLite's specific hot-journal error can justify
        # recovery. Never initialize, migrate or run arbitrary write statements.
        url = url.set(database=path.as_uri(), query={"mode": "ro", "uri": "true"})
    try:
        identity = await _has_identity(url)
    except OperationalError as exc:
        if (
            not can_recover
            or getattr(exc.orig, "sqlite_errorcode", None) != sqlite3.SQLITE_READONLY_ROLLBACK
        ):
            raise
        # A normal legacy crash must remain recoverable. Recheck ownership before
        # opening the existing file for SQLite's rollback; still no schema repair.
        await asyncio.to_thread(registry.check_unadopted, data_dir.resolve())
        await asyncio.to_thread(registry.check_unadopted, path)
        identity = await _has_identity(url.update_query_dict({"mode": "rw"}))
    if identity:
        raise ValueError("This database belongs to a profile that is not at this directory")


async def _has_identity(url) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            exists = await conn.run_sync(lambda c: inspect(c).has_table("profile_identity"))
            return bool(
                exists and await conn.scalar(text("SELECT 1 FROM profile_identity LIMIT 1"))
            )
    finally:
        await engine.dispose()
