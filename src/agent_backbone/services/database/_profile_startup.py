"""Read-only preflight before a legacy provider opens mutable storage."""

import asyncio
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.util import asbool

from agent_backbone.config import PROFILE_REGISTRY_PATH
from agent_backbone.services.database._profile_registry import ProfileRegistry
from agent_backbone.services.database.engine import is_memory


async def check_profile_startup(database_url: str, data_dir: Path) -> None:
    """Refuse reservations before mkdir, migrations or direct-mode agent starts.

    No profile can activate yet. Directory ownership catches pre-identity
    restores; any persisted identity catches copied databases outside that tree.
    This is a refusal guard, not identity attestation or running-service fencing.
    """
    registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
    await asyncio.to_thread(registry.check_unadopted, data_dir.resolve())
    url = make_url(database_url)
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
        # Discard URI write/recovery options: this preflight never mutates storage.
        url = url.set(database=path.as_uri(), query={"mode": "ro", "uri": "true"})
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            exists = await conn.run_sync(lambda c: inspect(c).has_table("profile_identity"))
            if exists and await conn.scalar(text("SELECT 1 FROM profile_identity LIMIT 1")):
                raise ValueError("Profile reservation is not ready; activation is not implemented")
    finally:
        await engine.dispose()
