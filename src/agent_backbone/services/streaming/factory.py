"""Streaming service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.streaming.interface import StreamingService


async def register_streaming(lifecycle: LifecycleManager) -> StreamingService:
    """Create and register the streaming service."""
    from agent_backbone.services.streaming.interface import StreamingService

    service = StreamingService()
    lifecycle.register("streaming", service)
    return service
