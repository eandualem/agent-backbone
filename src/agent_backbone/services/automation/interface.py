"""Automation service — LifecycleAware wrapper for repo onboarding."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.services.automation._checks import (
    RepoStatus,
)
from agent_backbone.services.automation._checks import (
    run_status_checks as _run_status_checks,
)
from agent_backbone.services.automation._pipeline import (
    OnboardingResult,
    RepoEntry,
)
from agent_backbone.services.automation._pipeline import (
    discover_repos as _discover_repos,
)
from agent_backbone.services.automation._pipeline import (
    run_onboarding as _run_onboarding,
)
from agent_backbone.services.automation._pipeline import (
    validate_org as _validate_org,
)
from agent_backbone.services.automation._pipeline import (
    validate_repo_name as _validate_repo_name,
)

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)


class OnboardingService:
    """Onboarding service implementing LifecycleAware."""

    async def start(self) -> None:
        log.info("Onboarding service started")

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "onboarding"}

    # --- DI surface for route handlers ---

    def discover_repos(self) -> list[RepoEntry]:
        """Discover all repos in the workspace."""
        return _discover_repos()

    def run_status_checks(self, org: str, repo: str) -> RepoStatus:
        """Run onboarding status checks for a repo."""
        return _run_status_checks(org, repo)

    def validate_org(self, org: str) -> bool:
        """Validate an organization name."""
        return _validate_org(org)

    def validate_repo_name(self, repo: str) -> bool:
        """Validate a repository name."""
        return _validate_repo_name(repo)

    async def run_onboarding(
        self, org: str, url: str, config: BackboneConfig | None = None
    ) -> OnboardingResult:
        """Run onboarding pipeline for a repo."""
        return await _run_onboarding(org, url, config=config)
