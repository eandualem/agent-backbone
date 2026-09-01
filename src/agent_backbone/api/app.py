"""FastAPI application factory.

One process hosts everything: the REST API, the Socket.IO feed, the periodic
scheduler (monitor, delivery retry, GitHub intake), the Telegram bot and the
GitHub client. Configuration comes from the database: the lifespan opens the
data directory, starts the database, loads settings and agents, and only then
wires the remaining services against that snapshot.
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
from agent_backbone.config import BackboneConfig, bootstrap_config

log = logging.getLogger(__name__)

API_VERSION = "2.0.0"


def _register_jobs(app: FastAPI):
    """Wire the periodic jobs. Each job reads ``app.state.config`` at run time so
    setting changes and newly discovered agents are picked up without a restart."""
    from agent_backbone.services.agents._monitor import monitor_agents
    from agent_backbone.services.routing._flows import delivery_retry
    from agent_backbone.services.scheduler import PeriodicScheduler

    scheduler = PeriodicScheduler()
    state = app.state
    config: BackboneConfig = state.config

    async def _monitor():
        await state.agent_store.refresh()
        return await monitor_agents(
            state.config,
            state.db,
            state.github,
            state_svc=state.state_service,
            tmux_svc=state.tmux_service,
            sio=getattr(state, "sio", None),
        )

    async def _retry():
        return await delivery_retry(state.config, state.db, state.github)

    async def _prune():
        days = state.config.delivery.retention_days
        return {
            "deliveries": await state.db.prune_old_deliveries(days),
            "events": await state.db.prune_events(days),
        }

    scheduler.add("agent-monitor", config.monitor.interval_seconds, _monitor)
    scheduler.add("delivery-retry", config.monitor.retry_interval_seconds, _retry)
    scheduler.add("prune", 6 * 3600, _prune)

    if state.github is not None:
        from agent_backbone.services.github._poller import GitHubPoller

        poller = GitHubPoller(
            lambda: state.config,
            state.db,
            state.github,
            state.delivery_service,
            state.dispatch_service,
        )
        if config.github_intake == "poll":
            scheduler.add(
                "github-poll", config.github.poll_interval_seconds, poller.run, run_immediately=True
            )
        elif config.github_intake == "webhook" and config.github.backfill_on_start:
            scheduler.add("github-backfill", 24 * 3600, poller.run, run_immediately=True)
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the data directory, start the database, load config, wire services."""
    from agent_backbone.api.socketio_server import configure_pty_manager, get_pty_manager
    from agent_backbone.services.agent_store import AgentStore
    from agent_backbone.services.agents import StateService
    from agent_backbone.services.agents._reconciliation import reconcile_startup_states
    from agent_backbone.services.database import BackboneDB, DatabaseService
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.routing import DeliveryService, DispatchService
    from agent_backbone.services.telegram import TelegramService
    from agent_backbone.services.terminal import TmuxService

    boot: BackboneConfig = getattr(app.state, "config", None) or bootstrap_config()
    data_dir = boot.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    app.state.config = boot
    configure_pty_manager(data_dir)

    lifecycle = LifecycleManager()
    app.state.lifecycle = lifecycle

    # Stage 1: the database, then the configuration stored in it.
    app.state.database_service = DatabaseService(boot.database_url)
    app.state.db = BackboneDB(database_service=app.state.database_service)
    lifecycle.register("database", app.state.database_service)
    lifecycle.register("persistence", app.state.db)
    await lifecycle.start_all()

    def _publish(new_config: BackboneConfig) -> None:
        app.state.config = new_config

    app.state.agent_store = AgentStore(app.state.db, data_dir, on_change=_publish)
    lifecycle.register("agents", app.state.agent_store)
    await lifecycle.start_all()
    config: BackboneConfig = app.state.config

    if config.github.intake == "webhook" and not config.webhook_secret:
        log.warning(
            "github.intake=webhook but GITHUB_WEBHOOK_SECRET is not set — "
            "webhooks would all be rejected, falling back to polling"
        )

    # Stage 2: everything else, against the loaded snapshot.
    app.state.github = GitHubClient(config) if config.github_ready else None
    if app.state.github is not None:
        lifecycle.register("github", app.state.github)
    app.state.tmux_service = TmuxService()
    lifecycle.register("tmux", app.state.tmux_service)
    app.state.state_service = StateService(
        config.state_dir,
        config.agent_state.stale_threshold_seconds,
        db=app.state.db,
        snapshot_trust=config.agent_state.snapshot_trust_seconds,
    )
    lifecycle.register("state", app.state.state_service)
    app.state.delivery_service = DeliveryService()
    lifecycle.register("delivery", app.state.delivery_service)
    app.state.dispatch_service = DispatchService()
    lifecycle.register("dispatch", app.state.dispatch_service)
    app.state.telegram_service = TelegramService(lambda: app.state.config, db=app.state.db)
    lifecycle.register("telegram", app.state.telegram_service)
    app.state.scheduler = _register_jobs(app)
    lifecycle.register("scheduler", app.state.scheduler)

    # A closed issue ends the swarm that was working it (PR merged -> issue
    # closed via "Closes #N" -> teardown). Wired here so routing stays a leaf.
    from agent_backbone.services.routing._ingest import register_issue_closed_listener
    from agent_backbone.services.swarm import teardown_for_issue

    async def _swarm_teardown(repo: str, issue_number: int) -> None:
        name = await teardown_for_issue(
            app.state.config, app.state.db, app.state.agent_store, repo, issue_number
        )
        if name:
            log.info("Swarm '%s' completed with %s#%s", name, repo, issue_number)

    register_issue_closed_listener(_swarm_teardown)

    try:
        await lifecycle.start_all()
        await reconcile_startup_states(config=config, db=app.state.db)
        log.info(
            "agent-backbone %s on http://%s:%d — data %s, %d agent(s), github=%s, telegram=%s",
            API_VERSION,
            config.backbone.host,
            config.backbone.port,
            data_dir,
            len(config.agents),
            config.github_intake,
            "on" if app.state.telegram_service.enabled else "off",
        )
        yield
    finally:
        await lifecycle.stop_all()
        await get_pty_manager().cleanup_all()
        log.info("agent-backbone shutting down")


def create_app(config: BackboneConfig | None = None) -> socketio.ASGIApp:
    """Build and return the ASGI application (Socket.IO wrapping FastAPI)."""
    config = config or bootstrap_config()
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
    from agent_backbone.api.routes.config import router as config_router
    from agent_backbone.api.routes.deliveries import router as deliveries_router
    from agent_backbone.api.routes.events import router as events_router
    from agent_backbone.api.routes.help import router as help_router
    from agent_backbone.api.routes.issues import router as issues_router
    from agent_backbone.api.routes.messages import router as messages_router
    from agent_backbone.api.routes.plans import router as plans_router
    from agent_backbone.api.routes.status import router as status_router
    from agent_backbone.api.routes.swarms import router as swarms_router
    from agent_backbone.api.routes.telegram import router as telegram_router
    from agent_backbone.api.routes.webhook import router as webhook_router

    app.include_router(webhook_router)

    for router in (
        agents_router,
        status_router,  # before config_router: /api/config/agents vs /api/config/{key}
        config_router,
        deliveries_router,
        events_router,
        help_router,
        issues_router,
        messages_router,
        plans_router,
        swarms_router,
        telegram_router,
    ):
        app.include_router(router, dependencies=[Depends(require_api_key)])

    from agent_backbone.api.socketio_server import create_sio

    sio = create_sio(cors_origins=cors_origins or [])
    sio.fastapi_app = app
    app.state.sio = sio
    return socketio.ASGIApp(sio, app)
