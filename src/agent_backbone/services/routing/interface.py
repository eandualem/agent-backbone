"""Routing service interfaces — LifecycleAware wrappers for dispatch and delivery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.services.routing._create_notify import (
    create_and_notify as _create_and_notify,
)
from agent_backbone.services.routing._dedup import (
    DEFAULT_DEDUP_SECONDS,
)
from agent_backbone.services.routing._dedup import (
    is_recent_notification as _is_recent_notification,
)
from agent_backbone.services.routing._lifecycle import on_issue_closed as _on_issue_closed
from agent_backbone.services.routing._priority import (
    compute_priority_score as _compute_priority_score,
)
from agent_backbone.services.routing._router import issue_dispatcher as _issue_dispatcher
from agent_backbone.services.routing.models import DispatchResult

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig, PriorityScoringConfig
    from agent_backbone.models import IssueData, IssueEvent
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)


class DispatchService:
    """Dispatch coordination service implementing LifecycleAware."""

    async def start(self) -> None:
        log.info("Dispatch service started")

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "dispatch"}

    async def on_issue_closed(
        self, event: IssueEvent, config: BackboneConfig, gh: GitHubClient, db: BackboneDB
    ) -> dict:
        """Handle issue closed event — close-then-next lifecycle."""
        return await _on_issue_closed(event, config, gh, db)

    async def issue_dispatcher(
        self,
        event: IssueEvent,
        config: BackboneConfig,
        db: BackboneDB,
        gh: GitHubClient | None = None,
    ) -> DispatchResult:
        """Route an issue event to target sessions."""
        return await _issue_dispatcher(event, config, db, gh)


class DeliveryService:
    """Delivery coordination service implementing LifecycleAware."""

    async def start(self) -> None:
        log.info("Delivery service started")

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "delivery"}

    def is_recent_notification(
        self,
        repo: str,
        issue_number: int,
        target: str,
        dedup_seconds: int = DEFAULT_DEDUP_SECONDS,
    ) -> bool:
        """Whether this issue was announced to this target within the dedup window."""
        return _is_recent_notification(repo, issue_number, target, dedup_seconds)

    def compute_priority_score(
        self, issue: IssueData, scoring_config: PriorityScoringConfig, dependents: int = 0
    ) -> float:
        """Compute priority score for an issue."""
        return _compute_priority_score(issue, scoring_config, dependents)

    async def create_and_notify(
        self,
        gh: GitHubClient,
        title: str,
        body: str,
        labels: list[str],
        config: BackboneConfig,
        *,
        repo: str,
        db: BackboneDB | None = None,
        flow_name: str = "",
    ) -> IssueData:
        """Create an issue and notify targets."""
        return await _create_and_notify(
            gh, title, body, labels, config, repo=repo, db=db, flow_name=flow_name
        )
