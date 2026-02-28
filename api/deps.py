"""FastAPI dependency injection — service-based lookups from app.state."""

from __future__ import annotations

from fastapi import Request

from agent_backbone.config import BackboneConfig
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.persistence import BackboneDB


def get_config(request: Request) -> BackboneConfig:
    """Retrieve BackboneConfig from app state (loaded at startup)."""
    return request.app.state.config


def get_db(request: Request) -> BackboneDB:
    """Retrieve the long-lived BackboneDB instance from app state."""
    return request.app.state.db


def get_github(request: Request) -> GitHubClient:
    """Retrieve the long-lived GitHubClient instance from app state."""
    return request.app.state.github
