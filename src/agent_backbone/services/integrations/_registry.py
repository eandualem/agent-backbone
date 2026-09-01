"""The set of integrations wired into a running backbone."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_backbone.config import BackboneConfig
from agent_backbone.services.integrations.base import Integration

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)


def build_integrations(
    config: Callable[[], BackboneConfig], db: BackboneDB | None = None
) -> Integrations:
    """Every integration the backbone ships, whether configured or not.

    Unconfigured ones stay inert (``enabled`` False, ``start`` a no-op) so
    health and status can still list them.
    """
    from agent_backbone.services.integrations.telegram import TelegramService

    return Integrations([TelegramService(config, db=db)])


class Integrations:
    """Ordered collection with fan-out helpers. Iterable; ``get`` by name."""

    def __init__(self, items: list[Integration]) -> None:
        self._items = list(items)
        self._background: set[asyncio.Task] = set()

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def get(self, name: str) -> Integration | None:
        return next((i for i in self._items if i.name == name), None)

    @property
    def enabled(self) -> list[Integration]:
        return [i for i in self._items if i.enabled]

    def health(self) -> dict[str, str]:
        """``{name: up | down | disabled}`` for the status endpoint."""
        return {
            i.name: ("up" if i.running else "down") if i.enabled else "disabled"
            for i in self._items
        }

    async def reply_to_agent(self, agent: str, text: str) -> dict[str, str]:
        """Post an agent's answer on every enabled integration.

        Per integration: ``posted``, ``no_surface`` (nothing there maps to
        this agent, e.g. no topic yet) or ``failed`` (a surface exists but
        posting raised) — callers must not confuse the last two.
        """
        results: dict[str, str] = {}
        for integration in self.enabled:
            try:
                ok = await integration.reply_to_agent(agent, text)
                results[integration.name] = "posted" if ok else "no_surface"
            except Exception:
                log.exception("%s reply_to_agent failed", integration.name)
                results[integration.name] = "failed"
        return results

    async def sync_agents(self) -> None:
        """Let every enabled integration re-provision its per-agent surfaces."""
        for integration in self.enabled:
            try:
                await integration.sync_agents()
            except Exception:
                log.exception("%s sync_agents failed (non-fatal)", integration.name)

    def schedule_sync(self) -> None:
        """Fire-and-forget ``sync_agents`` from a synchronous callback (config publish)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.sync_agents(), name="integrations-sync")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
