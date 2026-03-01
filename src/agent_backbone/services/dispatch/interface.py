"""Dispatch coordination service — LifecycleAware wrapper."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.services.dispatch._lifecycle import on_issue_closed as _on_issue_closed
from agent_backbone.services.dispatch._router import (
    DispatchResult,
)
from agent_backbone.services.dispatch._router import (
    issue_dispatcher as _issue_dispatcher,
)

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.models import IssueEvent
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.persistence import BackboneDB

log = logging.getLogger(__name__)


class DispatchService:
    """Dispatch coordination service implementing LifecycleAware."""

    async def start(self) -> None:
        """Start dispatch service."""
        log.info("Dispatch service started")

    async def stop(self) -> None:
        """Stop dispatch service."""
        pass

    async def health_check(self) -> dict:
        """Check dispatch service health."""
        return {"healthy": True, "service": "dispatch"}

    # --- DI surface for route handlers ---

    async def on_issue_closed(
        self, event: IssueEvent, config: BackboneConfig, gh: GitHubClient
    ) -> str:
        """Handle issue closed event — close-then-next lifecycle."""
        return await _on_issue_closed(event, config, gh)

    async def issue_dispatcher(
        self, event: IssueEvent, config: BackboneConfig, db: BackboneDB
    ) -> DispatchResult:
        """Route an issue event to target sessions."""
        return await _issue_dispatcher(event, config, db)
