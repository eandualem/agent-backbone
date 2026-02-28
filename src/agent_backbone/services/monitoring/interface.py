"""Monitoring coordination service — LifecycleAware wrapper."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class MonitoringService:
    """Monitoring coordination service implementing LifecycleAware."""

    async def start(self) -> None:
        """Start monitoring service."""
        log.info("Monitoring service started")

    async def stop(self) -> None:
        """Stop monitoring service."""
        pass

    async def health_check(self) -> dict:
        """Check monitoring service health."""
        return {"healthy": True, "service": "monitoring"}
