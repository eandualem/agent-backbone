"""The set of integrations wired into a running backbone."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING

from agent_backbone.config import BackboneConfig
from agent_backbone.services.integrations.base import Integration

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationDescriptor:
    name: str
    module: str
    service: str
    notifier: str

    def build(self, config, db):
        return getattr(import_module(self.module), self.service)(config, db=db)

    async def notify(self, config, text, agent, actions):
        send = getattr(import_module(self.module), self.notifier)
        return await send(config, text, agent=agent, actions=actions)


DESCRIPTORS = (
    IntegrationDescriptor(
        "telegram",
        "agent_backbone.services.integrations.telegram.interface",
        "TelegramService",
        "notify_static",
    ),
)


def build_integrations(
    config: Callable[[], BackboneConfig], db: BackboneDB | None = None
) -> Integrations:
    """Every integration the backbone ships, whether configured or not.

    Unconfigured ones stay inert (``enabled`` False, ``start`` a no-op) so
    health and status can still list them.
    """
    return Integrations([descriptor.build(config, db) for descriptor in DESCRIPTORS])


class Integrations:
    """Ordered collection with fan-out helpers. Iterable; ``get`` by name."""

    def __init__(self, items: list[Integration]) -> None:
        self._items = list(items)
        self._background: set[asyncio.Task] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._stopping = False
        self._resync_requested = False
        self._startup_keys: dict[str, object] = {}

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

    async def reconcile(self) -> None:
        """Serialize enable/disable transitions without restarting running services."""
        async with self._lifecycle_lock:
            if self._stopping:
                return
            for integration in self._items:
                try:
                    key = integration.startup_key
                    if integration.running and (
                        not integration.can_start or self._startup_keys.get(integration.name) != key
                    ):
                        await integration.stop()
                    if integration.can_start and not integration.running:
                        await integration.start()
                        if integration.running:
                            self._startup_keys[integration.name] = key
                except Exception:
                    log.exception("%s lifecycle reconciliation failed", integration.name)
            await self.sync_agents()

    async def stop(self) -> None:
        self._stopping = True
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        async with self._lifecycle_lock:
            for integration in reversed(self._items):
                await integration.stop()

    async def start(self) -> None:
        self._stopping = False
        await self.reconcile()

    async def health_check(self) -> dict:
        return {
            "healthy": all(i.running or not i.enabled for i in self._items),
            "integrations": self.health(),
        }

    async def sync_agents(self) -> None:
        """Let every enabled integration re-provision its per-agent surfaces."""
        for integration in self.enabled:
            try:
                await integration.sync_agents()
            except Exception:
                log.exception("%s sync_agents failed (non-fatal)", integration.name)

    def schedule_sync(self) -> None:
        """Fire-and-forget ``sync_agents`` from a synchronous callback (config publish)."""
        self._resync_requested = True
        if self._stopping or any(not t.done() for t in self._background):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def drain_changes():
            while self._resync_requested and not self._stopping:
                self._resync_requested = False
                await self.reconcile()

        task = loop.create_task(drain_changes(), name="integrations-sync")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def flush_reports(self) -> None:
        for integration in self.enabled:
            if integration.running:
                await integration.flush_reports()

    async def flush_report_audio(self) -> None:
        for integration in self.enabled:
            if integration.running:
                await integration.flush_report_audio()
