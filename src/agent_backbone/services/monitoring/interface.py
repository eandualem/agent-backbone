"""Monitoring coordination service — LifecycleAware wrapper."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_backbone.services.monitoring._heartbeat import (
    load_schedules as _load_schedules,
)
from agent_backbone.services.monitoring._heartbeat import (
    save_schedules as _save_schedules,
)

log = logging.getLogger(__name__)


class MonitoringService:
    """Monitoring coordination service implementing LifecycleAware."""

    def __init__(self, schedule_path: Path | None = None) -> None:
        self._schedule_path = schedule_path

    async def start(self) -> None:
        """Start monitoring service."""
        log.info("Monitoring service started")

    async def stop(self) -> None:
        """Stop monitoring service."""
        pass

    async def health_check(self) -> dict:
        """Check monitoring service health."""
        return {"healthy": True, "service": "monitoring"}

    # --- DI surface for route handlers ---

    def load_schedules(self) -> dict:
        """Load heartbeat schedules from JSON file."""
        return _load_schedules(self._schedule_path)

    def save_schedules(self, schedules: dict) -> None:
        """Save heartbeat schedules to JSON file."""
        _save_schedules(schedules, self._schedule_path)
