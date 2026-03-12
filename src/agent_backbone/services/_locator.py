"""Module-level service locator for scheduled flows.

Populated during app lifespan. Scheduled flows (cron) use these getters.
Webhook-triggered flows receive services via parameters instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.database.interface import DatabaseService
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

_config: BackboneConfig | None = None
_db: BackboneDB | None = None
_gh: GitHubClient | None = None
_database_service: DatabaseService | None = None
_init_lock = asyncio.Lock()


def init(
    *,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient,
    database_service: DatabaseService | None = None,
) -> None:
    """Populate the service locator. Called once during app lifespan."""
    global _config, _db, _gh, _database_service
    _config = config
    _db = db
    _gh = gh
    _database_service = database_service
    log.info("Flow services initialized")


async def ensure_initialized() -> None:
    """Lazily initialize flow services for worker-run Prefect subprocesses."""
    if _config is not None and _db is not None and _gh is not None:
        return

    async with _init_lock:
        if _config is not None and _db is not None and _gh is not None:
            return

        from agent_backbone.settings import AppSettings
        from agent_backbone.services.database.backbone_db import BackboneDB
        from agent_backbone.services.database.interface import DatabaseService
        from agent_backbone.services.github.interface import GitHubClient

        config = AppSettings().build_config()
        database_service = DatabaseService(config.database)
        await database_service.start()

        db = BackboneDB(database_service=database_service)
        await db.start()

        gh = GitHubClient(config)
        await gh.start()

        init(config=config, db=db, gh=gh, database_service=database_service)
        log.info("Flow services lazily initialized for worker process")


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


def reset() -> None:
    """Clear all services. Used for test isolation."""
    global _config, _db, _gh, _database_service
    _config = None
    _db = None
    _gh = None
    _database_service = None
