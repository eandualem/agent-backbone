"""Workflows service — LifecycleAware wrapper."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class WorkflowsService:
    """Workflows service implementing LifecycleAware."""

    async def start(self) -> None:
        log.info("Workflows service started")

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "workflows"}
