"""Onboarding service — LifecycleAware wrapper."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class OnboardingService:
    """Onboarding service implementing LifecycleAware."""

    async def start(self) -> None:
        log.info("Onboarding service started")

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "onboarding"}
