"""Protocols for the agent backbone foundation layer.

Defines structural contracts that components implement for lifecycle management.
"""

from __future__ import annotations

from typing import Protocol


class LifecycleAware(Protocol):
    """Component that participates in managed startup/shutdown."""

    async def start(self) -> None:
        """Initialize the component. Called during startup in registration order."""
        ...

    async def stop(self) -> None:
        """Tear down the component. Called during shutdown in reverse registration order."""
        ...

    async def health_check(self) -> dict:
        """Return component health status.

        Returns a dict with at least a ``"healthy"`` bool key.
        """
        ...
