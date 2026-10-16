"""The set of sources wired into a running backbone."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module

from agent_backbone.config import BackboneConfig
from agent_backbone.services.sources.base import Source


@dataclass(frozen=True)
class SourceDescriptor:
    name: str
    module: str
    service: str

    def build(self, config) -> Source:
        return getattr(import_module(self.module), self.service)(config)


DESCRIPTORS = (SourceDescriptor("gmail", "agent_backbone.services.sources.gmail", "GmailSource"),)


def build_sources(config: Callable[[], BackboneConfig]) -> Sources:
    """Every source the backbone ships, whether configured or not.

    Unconfigured ones stay inert (``enabled`` False) so health and status can
    still list them.
    """
    return Sources([descriptor.build(config) for descriptor in DESCRIPTORS])


class Sources:
    """Ordered collection. Iterable; ``get`` by name."""

    def __init__(self, items: list[Source]) -> None:
        self._items = list(items)

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def get(self, name: str) -> Source | None:
        return next((s for s in self._items if s.name == name), None)

    @property
    def enabled(self) -> list[Source]:
        return [s for s in self._items if s.enabled]

    def health(self) -> dict[str, str]:
        """``{name: enabled | disabled}`` for the status endpoint."""
        return {s.name: "enabled" if s.enabled else "disabled" for s in self._items}
