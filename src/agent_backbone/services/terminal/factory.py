"""Terminal service factories."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.terminal.interface import StreamingService, TmuxService


async def register_tmux(lifecycle: LifecycleManager) -> TmuxService:
    from agent_backbone.services.terminal.interface import TmuxService

    service = TmuxService()
    lifecycle.register("tmux", service)
    return service


async def register_streaming(lifecycle: LifecycleManager) -> StreamingService:
    """Create and register the streaming service."""
    from agent_backbone.services.terminal.interface import StreamingService

    service = StreamingService()
    lifecycle.register("streaming", service)
    return service
