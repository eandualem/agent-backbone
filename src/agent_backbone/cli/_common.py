"""Shared by every command: the API-first / database-fallback plumbing."""

from __future__ import annotations

import json
import logging
from typing import Any

from agent_backbone.config import BackboneConfig, bootstrap_config

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
