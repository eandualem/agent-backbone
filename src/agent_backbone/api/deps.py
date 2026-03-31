"""FastAPI dependency injection — service-based lookups from app.state."""

from __future__ import annotations

from fastapi import Request

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.automation import OnboardingService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.issues import IssueService
from agent_backbone.services.terminal import TmuxService


def get_config(request: Request) -> BackboneConfig:
    """Retrieve BackboneConfig from app state (loaded at startup)."""
    return request.app.state.config


def get_db(request: Request) -> BackboneDB:
    """Retrieve the long-lived BackboneDB instance from app state."""
    return request.app.state.db


def get_github(request: Request) -> GitHubClient:
    """Retrieve the long-lived GitHubClient instance from app state."""
    return request.app.state.github


def get_issue_service(request: Request) -> IssueService:
    """Retrieve the IssueService instance from app state."""
    return request.app.state.issue_service


def get_state_service(request: Request) -> StateService:
    """Retrieve the StateService instance from app state."""
    return request.app.state.state_service


def get_tmux_service(request: Request) -> TmuxService:
    """Retrieve the TmuxService instance from app state."""
    return request.app.state.tmux_service


def get_onboarding_service(request: Request) -> OnboardingService:
    """Retrieve the OnboardingService instance from app state."""
    return request.app.state.onboarding_service
