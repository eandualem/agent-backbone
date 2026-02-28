"""Dispatch service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.dispatch.interface import DispatchService


async def register_dispatch(lifecycle: LifecycleManager) -> DispatchService:
    """Create and register the dispatch service."""
    from agent_backbone.services.dispatch.interface import DispatchService

    service = DispatchService()
    lifecycle.register("dispatch", service)
    return service
