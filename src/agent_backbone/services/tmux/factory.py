"""Tmux service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.tmux.interface import TmuxService


async def register_tmux(lifecycle: LifecycleManager) -> TmuxService:
    from agent_backbone.services.tmux.interface import TmuxService

    service = TmuxService()
    lifecycle.register("tmux", service)
    return service
