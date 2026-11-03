"""Source contract — an inbound event stream agents subscribe to.

A source is where events from outside enter the backbone: a mailbox today,
a chat workspace or a ticket tracker tomorrow. GitHub intake is the sibling
(``services/github`` + ``jobs/github_poll``) and moves behind this contract
when it is next touched. Every source plugs in the same way, so nothing
else in the backbone knows a vendor:

* It reads the *live* configuration through a provider and is ``enabled``
  only when its credential is present; an unconfigured source is inert.
* ``poll(filters, since)`` asks the source for every event newer than
  ``since`` that matches any of ``filters`` — each written in the source's
  own query language and evaluated **by the source**, never by the backbone
  — and says which filters each event matched. Cost follows the number of
  filters and matched events, not the size of the source.
* An event is a *reference* (an id, who, what, when, a link), never a body:
  the agent reads the item through its own connector under its own rules.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from agent_backbone.config import BackboneConfig


@dataclass(frozen=True)
class SourceEvent:
    """One new item in a source, as a reference the agent can follow."""

    source: str
    id: str
    """The source's own id for the item (a Gmail message id)."""
    sender: str
    subject: str
    received_at: datetime
    link: str
    filters: frozenset[str]
    """The subscription filters this item matched."""


class Source:
    """Base class for an inbound source. Subclasses override what they support."""

    name: str = "source"
    """Short id used in envelopes (``[via:<name>]``), subscriptions, health and logs."""

    def __init__(self, config: BackboneConfig | Callable[[], BackboneConfig]) -> None:
        self._config_provider = config if callable(config) else (lambda: config)

    @property
    def config(self) -> BackboneConfig:
        """Always the latest published configuration snapshot."""
        return self._config_provider()

    @property
    def enabled(self) -> bool:
        """Whether the source is configured at all (credential present)."""
        return False

    async def poll(self, filters: Sequence[str], since: datetime) -> list[SourceEvent]:
        """Events newer than ``since`` matching any filter, each with the filters it matched."""
        return []
