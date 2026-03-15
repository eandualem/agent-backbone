"""Infrastructure service — LifecycleAware wrapper for OS-level infrastructure."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.services.infrastructure._agents import (
    list_agents as _list_agents,
)
from agent_backbone.services.infrastructure._agents import (
    start_agent as _start_agent,
)
from agent_backbone.services.infrastructure._agents import (
    start_all as _start_all,
)
from agent_backbone.services.infrastructure._agents import (
    start_group as _start_group,
)
from agent_backbone.services.infrastructure._agents import (
    start_orchestrators as _start_orchestrators,
)
from agent_backbone.services.infrastructure._agents import (
    start_org as _start_org,
)
from agent_backbone.services.infrastructure._agents import (
    stop_agent as _stop_agent,
)
from agent_backbone.services.infrastructure._agents import (
    stop_all_agents as _stop_all_agents,
)
from agent_backbone.services.infrastructure._backbone import (
    restart_backbone as _restart_backbone,
)
from agent_backbone.services.infrastructure._backbone import (
    restart_gateway as _restart_gateway,
)
from agent_backbone.services.infrastructure._backbone import (
    start_backbone as _start_backbone,
)
from agent_backbone.services.infrastructure._backbone import (
    start_gateway as _start_gateway,
)
from agent_backbone.services.infrastructure._backbone import (
    start_prefect as _start_prefect,
)
from agent_backbone.services.infrastructure._backbone import (
    start_telegram as _start_telegram,
)
from agent_backbone.services.infrastructure._backbone import (
    start_worker as _start_worker,
)
from agent_backbone.services.infrastructure._backbone import (
    stop_backbone as _stop_backbone,
)
from agent_backbone.services.infrastructure._backbone import (
    stop_gateway as _stop_gateway,
)
from agent_backbone.services.infrastructure._backbone import (
    stop_prefect as _stop_prefect,
)
from agent_backbone.services.infrastructure._backbone import (
    stop_telegram as _stop_telegram,
)
from agent_backbone.services.infrastructure._backbone import (
    stop_worker as _stop_worker,
)
from agent_backbone.services.infrastructure._status import show_status as _show_status

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)


class InfrastructureService:
    """Infrastructure service implementing LifecycleAware.

    Manages OS-level infrastructure: backbone process orchestration,
    port/PID management, and agent group operations.
    """

    def __init__(self, config: BackboneConfig) -> None:
        self._config = config

    async def start(self) -> None:
        """Start infrastructure service."""
        log.info("Infrastructure service started")

    async def stop(self) -> None:
        """Stop infrastructure service."""
        pass

    async def health_check(self) -> dict:
        """Check infrastructure health — reports which backbone processes are running."""
        from agent_backbone.services.infrastructure._processes import pid_for_port
        from agent_backbone.services.terminal import session_exists

        prefect_up = await session_exists("prefect")
        gateway_up = await pid_for_port(self._config.gateway.port) is not None
        worker_up = await session_exists("backbone-worker")
        telegram_up = await session_exists("telegram-bot")

        return {
            "healthy": prefect_up and gateway_up,
            "service": "infrastructure",
            "prefect": prefect_up,
            "gateway": gateway_up,
            "worker": worker_up,
            "telegram": telegram_up,
        }

    # --- DI surface for route handlers ---

    async def status(self) -> str:
        """Get formatted infrastructure status."""
        return await _show_status(self._config)

    async def start_agent(self, name: str, cli: str = "claude", model: str | None = None) -> bool:
        """Start a single agent session."""
        return await _start_agent(name, self._config, cli=cli, model=model)

    async def stop_agent(self, name: str) -> bool:
        """Stop a single agent session."""
        return await _stop_agent(name)

    async def start_group(
        self, label: str, names: list[str], cli: str = "claude", model: str | None = None
    ) -> int:
        """Start a group of agents."""
        return await _start_group(label, names, self._config, cli=cli, model=model)

    async def stop_all_agents(self) -> int:
        """Stop all agent sessions."""
        return await _stop_all_agents(self._config)

    async def start_orchestrators(self, cli: str = "claude", model: str | None = None) -> int:
        """Start all orchestrator agents."""
        return await _start_orchestrators(self._config, cli=cli, model=model)

    async def start_org(self, org: str, cli: str = "claude", model: str | None = None) -> int:
        """Start all coding agents for an organization."""
        return await _start_org(org, self._config, cli=cli, model=model)

    async def start_all(self, cli: str = "claude", model: str | None = None) -> int:
        """Start all agents."""
        return await _start_all(self._config, cli=cli, model=model)

    def list_agents(self) -> str:
        """Format list of all known agents."""
        return _list_agents(self._config)

    async def start_backbone(self) -> bool:
        """Start all backbone services."""
        return await _start_backbone(self._config)

    async def stop_backbone(self) -> bool:
        """Stop all backbone services."""
        return await _stop_backbone(self._config)

    async def restart_backbone(self) -> bool:
        """Restart all backbone services."""
        return await _restart_backbone(self._config)

    async def start_prefect(self) -> bool:
        return await _start_prefect(self._config)

    async def stop_prefect(self) -> bool:
        return await _stop_prefect(self._config)

    async def start_gateway(self) -> bool:
        return await _start_gateway(self._config)

    async def stop_gateway(self) -> bool:
        return await _stop_gateway(self._config)

    async def restart_gateway(self) -> bool:
        return await _restart_gateway(self._config)

    async def start_worker(self) -> bool:
        return await _start_worker(self._config)

    async def stop_worker(self) -> bool:
        return await _stop_worker(self._config)

    async def start_telegram(self) -> bool:
        return await _start_telegram(self._config)

    async def stop_telegram(self) -> bool:
        return await _stop_telegram(self._config)
