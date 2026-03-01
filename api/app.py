"""FastAPI application factory.

Creates the app with lifespan management, CORS, and router registration.
Services are registered with LifecycleManager for ordered startup/shutdown.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
import socketio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent_backbone.base import LifecycleManager
from agent_backbone.settings import AppSettings

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire services via LifecycleManager and manage startup/shutdown."""
    settings = AppSettings()
    config = settings.build_config()
    app.state.config = config
    app.state.settings = settings

    lifecycle = LifecycleManager()
    app.state.lifecycle = lifecycle

    # Register services in dependency order
    from agent_backbone.services.database.factory import register_database
    from agent_backbone.services.delivery.factory import register_delivery
    from agent_backbone.services.dispatch.factory import register_dispatch
    from agent_backbone.services.github.factory import register_github
    from agent_backbone.services.monitoring.factory import register_monitoring
    from agent_backbone.services.notifications.factory import register_notifications
    from agent_backbone.services.persistence.factory import register_persistence
    from agent_backbone.services.registry.factory import register_registry
    from agent_backbone.services.state.factory import register_state
    from agent_backbone.services.telegram.factory import register_telegram
    from agent_backbone.services.tmux.factory import register_tmux

    app.state.database_service = await register_database(lifecycle, config)
    app.state.db = await register_persistence(lifecycle, app.state.database_service)
    app.state.registry_service = await register_registry(lifecycle, config.registry)
    app.state.github = await register_github(lifecycle, config)
    app.state.tmux_service = await register_tmux(lifecycle)
    app.state.state_service = await register_state(lifecycle, config)
    app.state.notification_service = await register_notifications(lifecycle)
    app.state.delivery_service = await register_delivery(lifecycle)
    app.state.dispatch_service = await register_dispatch(lifecycle)
    app.state.monitoring_service = await register_monitoring(lifecycle, config)
    app.state.telegram_service = await register_telegram(lifecycle, config)

    # Non-lifecycle services (lightweight, no start/stop needed but exposed for DI)
    from agent_backbone.services.onboarding.interface import OnboardingService
    from agent_backbone.services.workflows.interface import WorkflowsService

    app.state.onboarding_service = OnboardingService()
    app.state.workflows_service = WorkflowsService()

    await lifecycle.start_all()

    # Populate the flow service locator for scheduled/cron flows
    from agent_backbone.services._locator import init as init_flow_services

    init_flow_services(config=config, db=app.state.db, gh=app.state.github)

    log.info("Backbone API started — port %d", config.gateway.port)
    yield

    await lifecycle.stop_all()

    # Non-lifecycle cleanup (stream broker, PTY)
    from api.broker import get_broker_instance

    broker = get_broker_instance()
    if broker is not None:
        await broker.shutdown()

    from api.socketio_server import get_pty_manager

    get_pty_manager().cleanup_all()
    log.info("Backbone API shutting down")


def create_app() -> socketio.ASGIApp:
    """Build and return the ASGI application (Socket.IO wrapping FastAPI)."""
    app = FastAPI(
        title="Agent Backbone API",
        description="REST API for agent orchestration backbone",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS for dashboard
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global exception handlers — prevent worker crashes on external API failures
    @app.exception_handler(httpx.HTTPError)
    async def httpx_error_handler(request: Request, exc: httpx.HTTPError):
        log.warning(
            "External API error on %s %s: %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            status_code=502,
            content={"detail": f"Upstream service error: {type(exc).__name__}"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        log.error(
            "Unhandled exception on %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # Health check (unauthenticated) — aggregates per-service health
    @app.get("/health")
    async def health(request: Request):
        lifecycle: LifecycleManager = request.app.state.lifecycle
        return await lifecycle.health()

    # Register routers
    from api.auth import require_api_key
    from api.routes.webhook import router as webhook_router

    app.include_router(webhook_router)

    # API routers (authenticated) — imported lazily to avoid circular imports
    from fastapi import Depends

    from api.routes.actions import router as actions_router
    from api.routes.activity import router as activity_router
    from api.routes.agents import router as agents_router
    from api.routes.deliveries import router as deliveries_router
    from api.routes.files import router as files_router
    from api.routes.heartbeats import router as heartbeats_router
    from api.routes.issues import router as issues_router
    from api.routes.notes import router as notes_router
    from api.routes.plans import router as plans_router
    from api.routes.prefect import router as prefect_router
    from api.routes.repos import router as repos_router
    from api.routes.rooms import router as rooms_router
    from api.routes.schedule import router as schedule_router
    from api.routes.status import router as status_router
    from api.routes.stream import router as stream_router
    from api.routes.workflows import router as workflows_router

    api_routers = [
        agents_router,
        deliveries_router,
        issues_router,
        plans_router,
        status_router,
        heartbeats_router,
        workflows_router,
        files_router,
        prefect_router,
        actions_router,
        schedule_router,
        activity_router,
        notes_router,
        rooms_router,
        repos_router,
        stream_router,
    ]
    for r in api_routers:
        app.include_router(r, dependencies=[Depends(require_api_key)])

    # Wrap FastAPI with Socket.IO — Socket.IO handles /socket.io/ path,
    # everything else falls through to FastAPI
    from api.socketio_server import create_sio

    sio = create_sio(cors_origins=["http://localhost:3000"])
    sio.fastapi_app = app  # Store reference for namespace config access
    app.state.sio = sio
    asgi_app = socketio.ASGIApp(sio, app)

    return asgi_app
