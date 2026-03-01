"""Delivery coordination service — LifecycleAware wrapper."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.services.delivery._create_notify import (
    create_and_notify as _create_and_notify,
)
from agent_backbone.services.delivery._dedup import (
    is_recent_notification as _is_recent_notification,
)
from agent_backbone.services.delivery._delivery import safe_deliver as _safe_deliver
from agent_backbone.services.delivery._priority import (
    compute_priority_score as _compute_priority_score,
)

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig, PriorityScoringConfig
    from agent_backbone.models import IssueData
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)


class DeliveryService:
    """Delivery coordination service implementing LifecycleAware."""

    async def start(self) -> None:
        """Start delivery service."""
        log.info("Delivery service started")

    async def stop(self) -> None:
        """Stop delivery service."""
        pass

    async def health_check(self) -> dict:
        """Check delivery service health."""
        return {"healthy": True, "service": "delivery"}

    # --- DI surface for route handlers ---

    def is_recent_notification(self, issue_number: int, target: str) -> bool:
        """Check if issue+target was already notified recently."""
        return _is_recent_notification(issue_number, target)

    def compute_priority_score(
        self, issue: IssueData, scoring_config: PriorityScoringConfig, dependents: int = 0
    ) -> float:
        """Compute priority score for an issue."""
        return _compute_priority_score(issue, scoring_config, dependents)

    async def safe_deliver(self, target: str, message: str, config: BackboneConfig) -> str:
        """Deliver a message with state pre-checks."""
        return await _safe_deliver(target, message, config)

    async def create_and_notify(
        self,
        gh: GitHubClient,
        title: str,
        body: str,
        labels: list[str],
        config: BackboneConfig,
        flow_name: str = "",
    ) -> IssueData:
        """Create an issue and notify targets."""
        return await _create_and_notify(gh, title, body, labels, config, flow_name=flow_name)
