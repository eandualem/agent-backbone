"""Onboarding service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.onboarding.interface import OnboardingService


async def register_onboarding(lifecycle: LifecycleManager) -> OnboardingService:
    """Create and register the onboarding service."""
    from agent_backbone.services.onboarding.interface import OnboardingService

    service = OnboardingService()
    lifecycle.register("onboarding", service)
    return service
