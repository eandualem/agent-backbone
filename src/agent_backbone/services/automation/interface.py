"""Automation service — LifecycleAware wrappers for onboarding and workflows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.automation._checks import (
    RepoStatus,
)
from agent_backbone.services.automation._checks import (
    run_status_checks as _run_status_checks,
)
from agent_backbone.services.automation._engine import (
    execute_workflow_steps as _execute_workflow_steps,
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
from agent_backbone.services.automation._registry import WorkflowRegistry

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

_JSON_WORKFLOW_DIR = Path.home() / ".claude" / "state" / "workflows"


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


class WorkflowsService:
    """Workflows service implementing LifecycleAware."""

    async def start(self) -> None:
        log.info("Workflows service started")

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "workflows"}

    # --- DI surface for route handlers ---

    def get_registry(self, json_dir: Path | None = None) -> WorkflowRegistry:
        """Get a fresh workflow registry with discovered workflows."""
        registry = WorkflowRegistry()
        registry.discover(json_dir=json_dir if json_dir is not None else _JSON_WORKFLOW_DIR)
        return registry

    async def execute_steps(self, steps: list[dict], config: BackboneConfig) -> dict:
        """Execute workflow steps via the engine."""
        return await _execute_workflow_steps(steps, config)
