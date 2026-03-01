"""Workflows service — LifecycleAware wrapper."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.workflows._engine import (
    execute_workflow_steps as _execute_workflow_steps,
)
from agent_backbone.services.workflows._registry import WorkflowRegistry

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

_JSON_WORKFLOW_DIR = Path.home() / ".claude" / "state" / "workflows"


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
