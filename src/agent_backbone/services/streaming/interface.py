"""Streaming service — LifecycleAware wrapper."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class StreamingService:
    """Streaming service implementing LifecycleAware."""

    async def start(self) -> None:
        log.info("Streaming service started")

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "streaming"}
