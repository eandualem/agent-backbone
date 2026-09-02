"""FastAPI dependency injection — service lookups from app.state."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException, Request

from agent_backbone.api.session_updates import SessionFeed
from agent_backbone.config import AgentSpec, BackboneConfig
from agent_backbone.services.agents import AgentStore
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.integrations import Integrations
from agent_backbone.services.routing import IssueClosedHook
from agent_backbone.services.scheduler import PeriodicScheduler


def get_config(request: Request) -> BackboneConfig:
    return request.app.state.config


def get_db(request: Request) -> BackboneDB:
    return request.app.state.db


def registered_agent_or_404(config: BackboneConfig, name: str) -> AgentSpec:
    """The backbone reads from and types into its *registered* agents only.

    Other tmux sessions of the same OS user are out of the API's reach even
    though the process could technically capture or type into them: the API
    key is a backbone credential, not a shell on the machine.
    """
    spec = config.agents.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"'{name}' is not a registered agent")
    return spec


def get_agent_store(request: Request) -> AgentStore:
    return request.app.state.agent_store


def get_github(request: Request) -> GitHubClient:
    """The GitHub client, or 503 when GitHub is not configured."""
    gh = getattr(request.app.state, "github", None)
    if gh is None:
        raise HTTPException(
            status_code=503,
            detail="GitHub is not configured — set GITHUB_TOKEN (or GitHub App credentials)",
        )
    return gh


def get_optional_github(request: Request) -> GitHubClient | None:
    return getattr(request.app.state, "github", None)


def get_feed(request: Request) -> SessionFeed:
    return request.app.state.feed


def get_issue_closed_hooks(request: Request) -> Sequence[IssueClosedHook]:
    return getattr(request.app.state, "issue_closed_hooks", ())


def get_scheduler(request: Request) -> PeriodicScheduler | None:
    return getattr(request.app.state, "scheduler", None)


def get_integrations(request: Request) -> Integrations | None:
    return getattr(request.app.state, "integrations", None)
