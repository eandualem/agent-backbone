"""Dispatch coordination service — LifecycleAware wrapper."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class DispatchService:
    """Dispatch coordination service implementing LifecycleAware."""

    async def start(self) -> None:
        """Start dispatch service."""
        log.info("Dispatch service started")

    async def stop(self) -> None:
        """Stop dispatch service."""
        pass

    async def health_check(self) -> dict:
        """Check dispatch service health."""
        return {"healthy": True, "service": "dispatch"}
