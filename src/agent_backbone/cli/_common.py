"""Shared by every command: the API-first / database-fallback plumbing."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_backbone.config import BackboneConfig, bootstrap_config, validate_setting

log = logging.getLogger(__name__)


_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def api_url(config: BackboneConfig, path: str) -> str:
    """The API's URL for ``path``.

    A loopback host is plain HTTP. Any other host is reached over HTTPS:
    the bearer token travels with every call, and the documented way to
    expose the backbone beyond the machine is behind TLS
    (``backbone.host`` help text).
    """
    scheme = "http" if config.backbone.host in _LOOPBACK else "https"
    return f"{scheme}://{config.backbone.host}:{config.backbone.port}{path}"


def headers(config: BackboneConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}


async def api(
    config: BackboneConfig, method: str, path: str, *, json_body: Any = None, timeout: float = 10.0
) -> tuple[int, Any] | None:
    """Call the running API: ``(status, payload)``, or None when it is not reachable.

    The payload is the decoded JSON body (a mapping or a list). A body that
    is not JSON — a proxy error page, an uvicorn crash — comes back as
    ``{"detail": text}`` so callers can always read ``detail``.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, api_url(config, path), headers=headers(config), json=json_body
            )
    except httpx.HTTPError:
        return None
    try:
        data = resp.json()
    except ValueError:
        data = {"detail": resp.text}
    return resp.status_code, data


async def api_up(config: BackboneConfig) -> bool:
    result = await api(config, "GET", "/health", timeout=3.0)
    return result is not None


class Direct:
    """Direct database access for when the backbone is not running."""

    def __init__(self, config: BackboneConfig) -> None:
        self._boot = config
        self.db = None
        self.store = None
        self.config = config

    async def __aenter__(self) -> Direct:
        from agent_backbone.services.agents import AgentStore
        from agent_backbone.services.database import BackboneDB

        self.db = BackboneDB(self._boot.database_url)
        await self.db.start()
        self.store = AgentStore(self.db, self._boot.data_dir)
        self.config = await self.store.refresh()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.db.stop()


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


async def load_config() -> BackboneConfig:
    """Full configuration (settings + agents) from the database."""
    async with Direct(bootstrap_config()) as direct:
        return direct.config


async def client_config() -> BackboneConfig:
    """Configuration for talking to the running API.

    ``backbone.host``/``backbone.port`` may be stored in the database, so the
    bootstrap defaults alone could point at the wrong address. Falls back to
    the bootstrap snapshot when the database cannot be read.
    """
    try:
        return await load_config()
    except Exception:
        return bootstrap_config()


def parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


_CLIENT_SETTINGS_SQL = (
    "SELECT key, value FROM settings WHERE key IN ('backbone.host', 'backbone.port')"
)


async def read_client_config() -> BackboneConfig:
    """Read only existing address settings, without creating or repairing a database."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    boot = bootstrap_config()
    try:
        url = make_url(boot.database_url)
        if url.get_backend_name() == "sqlite":
            if not url.database or url.database == ":memory:":
                return boot

            def sqlite_settings():
                uri = Path(url.database).expanduser().resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=1)
                try:
                    return conn.execute(_CLIENT_SETTINGS_SQL).fetchall()
                finally:
                    conn.close()

            rows = await asyncio.to_thread(sqlite_settings)
        else:
            engine = create_async_engine(boot.database_url)
            try:

                async def server_settings():
                    async with engine.connect() as conn:
                        return (await conn.execute(text(_CLIENT_SETTINGS_SQL))).fetchall()

                rows = await asyncio.wait_for(server_settings(), timeout=3)
            finally:
                await engine.dispose()
        settings = {key: validate_setting(key, json.loads(value)) for key, value in rows}
    except Exception:
        # A missing database or unreadable settings must not trigger initialization.
        # The API call will report whether the bootstrap address is reachable.
        return boot
    return replace(
        boot,
        backbone=replace(
            boot.backbone,
            host=settings.get("backbone.host", boot.backbone.host),
            port=settings.get("backbone.port", boot.backbone.port),
        ),
    )
