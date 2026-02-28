"""Delivery coordination service — LifecycleAware wrapper."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class DeliveryService:
    """Delivery coordination service implementing LifecycleAware."""

    async def start(self) -> None:
        """Start delivery service."""
        log.info("Delivery service started")

    async def stop(self) -> None:
        """Stop delivery service."""
        pass

    async def health_check(self) -> dict:
        """Check delivery service health."""
        return {"healthy": True, "service": "delivery"}
