"""FastAPI dependency injection — config, database, GitHub client."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends, Request

from src.config import BackboneConfig
from src.github import GitHubClient
from src.persistence import BackboneDB


def get_config(request: Request) -> BackboneConfig:
    """Retrieve BackboneConfig from app state (loaded at startup)."""
    return request.app.state.config


async def get_db(config: BackboneConfig = Depends(get_config)) -> AsyncGenerator[BackboneDB]:
    """Yield an async BackboneDB connection scoped to the request."""
    async with BackboneDB(str(config.delivery.db_file)) as db:
        yield db


async def get_github(config: BackboneConfig = Depends(get_config)) -> AsyncGenerator[GitHubClient]:
    """Yield an async GitHubClient scoped to the request."""
    async with GitHubClient(config) as gh:
        yield gh
