"""Integration contract — a human-facing channel the backbone talks through.

An integration is where people meet their agents outside the terminal:
Telegram today; Slack, Discord, e-mail or a web inbox tomorrow. Every one
plugs in the same way, so nothing else in the backbone knows a vendor:

* It is a lifecycle component started with the backbone (``start`` /
  ``stop`` / ``health_check``) that reads the *live* configuration through
  a provider — settings changes are picked up without a restart.
* Inbound text from a person becomes an ordinary delivery through
  ``safe_deliver`` with a ``[via:<integration> from:<who>]`` envelope. An
  integration never pastes into a terminal itself.
* ``reply_to_agent`` posts an agent's answer into whatever surface that
  agent has on the integration (a Telegram topic, a Slack thread).
* ``notify`` pushes an alert to the humans — into the agent's surface when
  there is one, else the integration's general alert destination.
* ``sync_agents`` runs whenever the set of registered agents changes (and
  periodically) so the integration can provision per-agent surfaces.

Config-driven static helpers (``notify_humans``) exist for callers that run
without an instance, such as scheduler jobs; see ``_notify.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_backbone.config import BackboneConfig

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB


class Integration:
    """Base class for a human-facing channel. Subclasses override what they support."""

    name: str = "integration"
    """Short id used in envelopes (``[via:<name> …]``), health and logs."""

    def __init__(
        self,
        config: BackboneConfig | Callable[[], BackboneConfig],
        db: BackboneDB | None = None,
    ) -> None:
        self._config_provider = config if callable(config) else (lambda: config)
        self._db = db
        self._running = False

    @property
    def config(self) -> BackboneConfig:
        """Always the latest published configuration snapshot."""
        return self._config_provider()

    @property
    def enabled(self) -> bool:
        """Whether the integration is configured at all (credentials present)."""
        return False

    @property
    def running(self) -> bool:
        return self._running

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:  # pragma: no cover - trivial default
        return None

    async def stop(self) -> None:  # pragma: no cover - trivial default
        return None

    async def health_check(self) -> dict:
        return {
            "healthy": self._running or not self.enabled,
            "service": self.name,
            "enabled": self.enabled,
            "running": self._running,
        }

    # -- capabilities ----------------------------------------------------

    async def reply_to_agent(self, agent: str, text: str) -> bool:
        """Post ``text`` into the agent's surface. False when it has none here."""
        return False

    async def notify(
        self,
        text: str,
        *,
        agent: str | None = None,
        actions: list[tuple[str, str]] | None = None,
    ) -> bool:
        """Push an alert to the humans, into ``agent``'s surface when it has one.
        ``actions`` are ``(label, callback data)`` buttons a channel may offer."""
        return False

    async def sync_agents(self) -> None:
        """Provision / retire per-agent surfaces to match the registered agents."""
        return None
