"""Module-level service locator for scheduled flows.

Populated during app lifespan. Scheduled flows (cron) use these getters.
Webhook-triggered flows receive services via parameters instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import socketio

    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

_config: BackboneConfig | None = None
_db: BackboneDB | None = None
_gh: GitHubClient | None = None
_sio: socketio.AsyncServer | None = None


def init(
    *,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient,
    sio: socketio.AsyncServer | None = None,
) -> None:
    """Populate the service locator. Called once during app lifespan."""
    global _config, _db, _gh, _sio
    _config = config
    _db = db
    _gh = gh
    _sio = sio
    log.info("Flow services initialized")


def get_config() -> BackboneConfig:
    """Return the shared BackboneConfig instance."""
    if _config is None:
        raise RuntimeError("Flow services not initialized — call init() during lifespan")
    return _config


def get_db() -> BackboneDB:
    """Return the shared BackboneDB instance."""
    if _db is None:
        raise RuntimeError("Flow services not initialized — call init() during lifespan")
    return _db


def get_gh() -> GitHubClient:
    """Return the shared GitHubClient instance."""
    if _gh is None:
        raise RuntimeError("Flow services not initialized — call init() during lifespan")
    return _gh


def get_sio() -> socketio.AsyncServer | None:
    """Return the shared Socket.IO server when running inside the gateway process."""
    return _sio


def reset() -> None:
    """Clear all services. Used for test isolation."""
    global _config, _db, _gh, _sio
    _config = None
    _db = None
    _gh = None
    _sio = None
