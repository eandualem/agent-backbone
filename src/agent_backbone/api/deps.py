"""FastAPI dependency injection — service lookups from app.state."""

from __future__ import annotations

from fastapi import HTTPException, Request

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.routing import DeliveryService, DispatchService
from agent_backbone.services.scheduler import PeriodicScheduler
from agent_backbone.services.telegram import TelegramService
from agent_backbone.services.terminal import TmuxService


def get_config(request: Request) -> BackboneConfig:
    return request.app.state.config


def get_db(request: Request) -> BackboneDB:
    return request.app.state.db


def get_github(request: Request) -> GitHubClient:
    """The GitHub client, or 503 when no tracker is configured."""
    gh = getattr(request.app.state, "github", None)
    if gh is None:
        raise HTTPException(
            status_code=503,
            detail="GitHub is not configured — set [github] repo and GITHUB_TOKEN",
        )
    return gh


def get_optional_github(request: Request) -> GitHubClient | None:
    return getattr(request.app.state, "github", None)


def get_state_service(request: Request) -> StateService:
    return request.app.state.state_service


def get_tmux_service(request: Request) -> TmuxService:
    return request.app.state.tmux_service


def get_delivery_service(request: Request) -> DeliveryService:
    return request.app.state.delivery_service


def get_dispatch_service(request: Request) -> DispatchService:
    return request.app.state.dispatch_service


def get_scheduler(request: Request) -> PeriodicScheduler | None:
    return getattr(request.app.state, "scheduler", None)


def get_telegram_service(request: Request) -> TelegramService | None:
    return getattr(request.app.state, "telegram_service", None)
