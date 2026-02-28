"""Workflows service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.workflows.interface import WorkflowsService


async def register_workflows(lifecycle: LifecycleManager) -> WorkflowsService:
    """Create and register the workflows service."""
    from agent_backbone.services.workflows.interface import WorkflowsService

    service = WorkflowsService()
    lifecycle.register("workflows", service)
    return service
