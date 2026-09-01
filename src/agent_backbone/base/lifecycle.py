"""Lifecycle manager for backbone components.

Manages ordered startup, graceful shutdown, and health aggregation
for components implementing the LifecycleAware protocol.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from agent_backbone.base.protocols import LifecycleAware

_log = logging.getLogger(__name__)


class LifecycleManager:
    """Manages startup/shutdown ordering for LifecycleAware components.

    Registration order determines startup order. Shutdown is reverse order.
    On startup failure, already-started components are rolled back (stopped in reverse).
    """

    def __init__(self) -> None:
        self._components: OrderedDict[str, LifecycleAware] = OrderedDict()
        self._started: list[str] = []

    def register(self, name: str, component: LifecycleAware) -> None:
        """Register a component. Raises ValueError on duplicate name."""
        if name in self._components:
            raise ValueError(f"Component already registered: {name}")
        self._components[name] = component

    async def start_all(self) -> None:
        """Start all not-yet-started components in registration order.

        Safe to call repeatedly during staged startup: components already
        started are left alone. On failure, rolls back every started
        component in reverse order.
        """
        for name, component in self._components.items():
            if name in self._started:
                continue
            try:
                _log.info("Starting component: %s", name)
                await component.start()
                self._started.append(name)
            except Exception:
                _log.exception("Failed to start component: %s — rolling back", name)
                await self.stop_all(reason="Rolling back")
                raise

    async def stop_all(self, *, reason: str = "Stopping") -> None:
        """Stop all started components in reverse order.

        Logs errors but continues stopping remaining components.
        """
        for name in reversed(self._started):
            component = self._components[name]
            try:
                _log.info("%s component: %s", reason, name)
                await component.stop()
            except Exception:
                _log.exception("Error stopping component: %s", name)
        self._started.clear()

    async def health(self) -> dict:
        """Aggregate health from all registered components.

        Returns ``{"healthy": bool, "components": {name: health_dict}}``.
        """
        components: dict[str, dict] = {}
        all_healthy = True
        for name, component in self._components.items():
            try:
                h = await component.health_check()
                components[name] = h
                if not h.get("healthy", False):
                    all_healthy = False
            except Exception as exc:
                components[name] = {"healthy": False, "error": str(exc)}
                all_healthy = False
        return {"healthy": all_healthy, "components": components}
