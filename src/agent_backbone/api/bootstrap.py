"""Application bootstrap helpers for API startup and shutdown."""

from __future__ import annotations

from fastapi import FastAPI

from agent_backbone.base import LifecycleManager
from agent_backbone.config import BackboneConfig
from agent_backbone.settings import AppSettings


def load_settings_and_config() -> tuple[AppSettings, BackboneConfig]:
    """Build application settings and the derived runtime config."""
    settings = AppSettings()
    return settings, settings.build_config()


def attach_runtime_config(app: FastAPI) -> BackboneConfig:
    """Store settings and config on the FastAPI app state."""
    settings, config = load_settings_and_config()
    app.state.settings = settings
    app.state.config = config
    return config


def attach_lifecycle(app: FastAPI) -> LifecycleManager:
    """Create and store the application lifecycle manager."""
    lifecycle = LifecycleManager()
    app.state.lifecycle = lifecycle
    return lifecycle


async def register_lifecycle_services(app: FastAPI) -> None:
    """Register lifecycle-managed services on app state."""
    config: BackboneConfig = app.state.config
    lifecycle = attach_lifecycle(app)

    # Register services in dependency order.
    from agent_backbone.services.agents.factory import register_monitoring, register_state
    from agent_backbone.services.database.factory import register_database, register_persistence
    from agent_backbone.services.github.factory import register_github
    from agent_backbone.services.infrastructure.factory import register_infrastructure
    from agent_backbone.services.registry.factory import register_registry
    from agent_backbone.services.routing.factory import (
        register_delivery,
        register_dispatch,
        register_notifications,
    )
    from agent_backbone.services.telegram.factory import register_telegram
    from agent_backbone.services.terminal.factory import register_tmux

    app.state.database_service = await register_database(lifecycle, config)
    app.state.db = await register_persistence(lifecycle, app.state.database_service)
    app.state.registry_service = await register_registry(lifecycle, config.registry)
    app.state.github = await register_github(lifecycle, config)
    app.state.tmux_service = await register_tmux(lifecycle)
    app.state.state_service = await register_state(lifecycle, config, db=app.state.db)
    app.state.notification_service = await register_notifications(lifecycle)
    app.state.delivery_service = await register_delivery(lifecycle)
    app.state.dispatch_service = await register_dispatch(lifecycle)
    app.state.monitoring_service = await register_monitoring(lifecycle, config)
    app.state.telegram_service = await register_telegram(lifecycle, config)
    app.state.infrastructure_service = await register_infrastructure(lifecycle, config)


def register_lightweight_services(app: FastAPI) -> None:
    """Register lightweight services that do not use lifecycle hooks."""
    from agent_backbone.services.automation.interface import OnboardingService, WorkflowsService

    app.state.onboarding_service = OnboardingService()
    app.state.workflows_service = WorkflowsService()


async def start_runtime_services(app: FastAPI) -> None:
    """Start lifecycle services and initialize process-level integrations."""
    config: BackboneConfig = app.state.config
    lifecycle: LifecycleManager = app.state.lifecycle

    await lifecycle.start_all()

    from agent_backbone.services._locator import init as init_flow_services
    from agent_backbone.services.agents._reconciliation import reconcile_startup_states

    init_flow_services(config=config, db=app.state.db, gh=app.state.github)
    await reconcile_startup_states(config=config, db=app.state.db)


async def stop_runtime_services(app: FastAPI) -> None:
    """Stop all lifecycle-managed services if they were initialized."""
    lifecycle: LifecycleManager | None = getattr(app.state, "lifecycle", None)
    if lifecycle is not None:
        await lifecycle.stop_all()
