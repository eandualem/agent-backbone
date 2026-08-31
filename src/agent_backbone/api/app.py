"""FastAPI application factory.

One process hosts everything: the REST API, the Socket.IO feed, the periodic
scheduler (monitor + delivery retry), the Telegram bot and the GitHub
connector. Services are registered with LifecycleManager for ordered
startup/shutdown.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
import socketio
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent_backbone.base import LifecycleManager
from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

API_VERSION = "2.0.0"


def _register_jobs(app: FastAPI, config: BackboneConfig):
    """Wire the periodic jobs that replace the old Prefect deployments."""
    from agent_backbone.services.agents._monitor import monitor_agents
    from agent_backbone.services.routing._flows import delivery_retry
    from agent_backbone.services.scheduler import PeriodicScheduler

    scheduler = PeriodicScheduler()
    state = app.state

    async def _monitor():
        return await monitor_agents(
            config,
            state.db,
            getattr(state, "github", None),
            state_svc=state.state_service,
            tmux_svc=state.tmux_service,
            sio=getattr(state, "sio", None),
        )

    async def _retry():
        return await delivery_retry(config, state.db, getattr(state, "github", None))

    async def _prune():
        pruned = await state.db.prune_old_deliveries(config.delivery.retention_days)
        ids = await state.db.prune_delivery_ids(max_age_hours=24)
        return {"deliveries_pruned": pruned, "delivery_ids_pruned": ids}

    scheduler.add("agent-monitor", config.monitor.interval_seconds, _monitor)
    scheduler.add("delivery-retry", config.monitor.retry_interval_seconds, _retry)
    scheduler.add("prune", 6 * 3600, _prune)
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire services via LifecycleManager and manage startup/shutdown."""
    config: BackboneConfig = getattr(app.state, "config", None) or BackboneConfig.load()
    app.state.config = config
    config.data_dir.mkdir(parents=True, exist_ok=True)

    from agent_backbone.services.infrastructure._processes import set_pid_dir

    set_pid_dir(config.data_dir / "pids")

    lifecycle = LifecycleManager()
    app.state.lifecycle = lifecycle

    from agent_backbone.services.agents.factory import register_state
    from agent_backbone.services.database.factory import register_database, register_persistence
    from agent_backbone.services.github.factory import register_github
    from agent_backbone.services.routing.factory import (
        register_delivery,
        register_dispatch,
        register_notifications,
    )
    from agent_backbone.services.telegram.factory import register_telegram
    from agent_backbone.services.terminal.factory import register_tmux

    app.state.database_service = await register_database(lifecycle, config)
    app.state.db = await register_persistence(lifecycle, app.state.database_service)
    app.state.github = None
    if config.github_ready:
        app.state.github = await register_github(lifecycle, config)
    elif config.github.enabled:
        log.warning(
            "[github] repo is set but no credentials found — set GITHUB_TOKEN "
            "(or GitHub App credentials). Issue routing is disabled."
        )
    app.state.tmux_service = await register_tmux(lifecycle)
    app.state.state_service = await register_state(lifecycle, config, db=app.state.db)
    app.state.notification_service = await register_notifications(lifecycle)
    app.state.delivery_service = await register_delivery(lifecycle)
    app.state.dispatch_service = await register_dispatch(lifecycle)
    app.state.telegram_service = await register_telegram(lifecycle, config, db=app.state.db)
    app.state.scheduler = _register_jobs(app, config)
    lifecycle.register("scheduler", app.state.scheduler)

    try:
        await lifecycle.start_all()

        from agent_backbone.services.agents._reconciliation import reconcile_startup_states

        await reconcile_startup_states(config=config, db=app.state.db)

        log.info(
            "agent-backbone %s listening on http://%s:%d — %d agent(s) configured",
            API_VERSION,
            config.backbone.host,
            config.backbone.port,
            len(config.agents),
        )
        yield
    finally:
        await lifecycle.stop_all()

        from agent_backbone.api.socketio_server import get_pty_manager

        await get_pty_manager().cleanup_all()
        log.info("agent-backbone shutting down")


def create_app(config: BackboneConfig | None = None) -> socketio.ASGIApp:
    """Build and return the ASGI application (Socket.IO wrapping FastAPI)."""
    config = config or BackboneConfig.load()
    app = FastAPI(
        title="agent-backbone",
        description="Local control plane for terminal AI agents",
        version=API_VERSION,
        lifespan=lifespan,
    )
    app.state.config = config

    cors_origins = list(config.backbone.cors_origins)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

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
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.get("/health")
    async def health(request: Request):
        lifecycle: LifecycleManager | None = getattr(request.app.state, "lifecycle", None)
        if lifecycle is None:
            return {"healthy": False, "components": {}}
        return await lifecycle.health()

    from agent_backbone.api.auth import require_api_key
    from agent_backbone.api.routes.agents import router as agents_router
    from agent_backbone.api.routes.deliveries import router as deliveries_router
    from agent_backbone.api.routes.issues import router as issues_router
    from agent_backbone.api.routes.messages import router as messages_router
    from agent_backbone.api.routes.plans import router as plans_router
    from agent_backbone.api.routes.status import router as status_router
    from agent_backbone.api.routes.telegram import router as telegram_router
    from agent_backbone.api.routes.webhook import router as webhook_router

    app.include_router(webhook_router)

    for router in (
        agents_router,
        deliveries_router,
        issues_router,
        messages_router,
        plans_router,
        status_router,
        telegram_router,
    ):
        app.include_router(router, dependencies=[Depends(require_api_key)])

    from agent_backbone.api.socketio_server import create_sio

    sio = create_sio(cors_origins=cors_origins or [])
    sio.fastapi_app = app
    app.state.sio = sio
    return socketio.ASGIApp(sio, app)
