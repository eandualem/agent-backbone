"""FastAPI application factory.

One process hosts everything: the REST API, the Socket.IO feed, the periodic
scheduler (monitor, delivery retry, GitHub intake), the integrations (the
Telegram bot) and the GitHub client. Configuration comes from the database: the lifespan opens the
data directory, starts the database, loads settings and agents, and only then
wires the remaining services against that snapshot.
"""

from __future__ import annotations

import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar

import httpx
import socketio
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent_backbone import __version__
from agent_backbone.base import LifecycleManager, run_thread
from agent_backbone.config import BackboneConfig, agent_credential, bootstrap_config
from agent_backbone.hooks.backbone_state import batch_receipts, prune_context_receipts

log = logging.getLogger(__name__)

API_VERSION = __version__  # the package version is the API version
STARTED_AT = time.time()
"""When this process started: /health reports it so a restart is observable."""


async def hint_inboxes(config, db, feed) -> None:
    """The monitor tick's inbox hints: rows written outside a tell (expiry
    notices, escalations to an inbox-only agent) reach their hint here."""
    inbox = tuple(spec.name for spec in config.agents if spec.inbox_only)
    await feed.hint_inbox(lambda: db.queue.inbox_rows(inbox_sessions=inbox), complete=True)


def _register_jobs(app: FastAPI):
    """Wire the periodic jobs. Each job reads ``app.state.config`` at run time so
    setting changes and newly discovered agents are picked up without a restart."""
    from agent_backbone.services.agents import rotate_action_log
    from agent_backbone.services.github import QueueSnapshot
    from agent_backbone.services.jobs import (
        GitHubPoller,
        SourcesPoller,
        UpgradeWatch,
        delivery_retry,
        monitor_agents,
        observe_duration,
        observe_job,
        run_transitions,
        sample_resources,
        take_pass_steps,
    )
    from agent_backbone.services.jobs.transitions import INTERVAL_SECONDS as _TRANSITION_TICK
    from agent_backbone.services.routing import (
        routing_in_flight,
        settle_steers,
        sync_dependencies,
    )
    from agent_backbone.services.scheduler import PeriodicScheduler

    state = app.state
    from agent_backbone.api.profile_binding import provider_context_current

    provider = getattr(state, "profile_provider", None)
    db = state.db
    github = state.github
    store = getattr(state, "agent_store", None)
    captured_config = ContextVar("job_config")

    def current_config():
        return captured_config.get(state.config)

    def guarded(fn):
        async def run(*args):
            config = state.config
            token = captured_config.set(config)
            try:
                async with AsyncExitStack() as operations:
                    if (
                        getattr(state, "profile_provider", None) is not provider
                        or state.db is not db
                    ):
                        raise ValueError("Job provider context does not match")
                    if provider is not None:
                        await operations.enter_async_context(provider.operation())
                        if not await provider_context_current(provider, config, db):
                            raise ValueError("Job provider context does not match")
                    return await fn(*args)
            finally:
                captured_config.reset(token)

        return run

    async def _job_result(name: str, error_type: str | None, duration_ms: int):
        await observe_job(
            db, source=name, stage="run", error_type=error_type, duration_ms=duration_ms
        )
        slow = current_config().settings.get("diagnostics.slow_job_seconds", 30)
        await observe_duration(
            db,
            source=name,
            duration_ms=duration_ms,
            threshold_ms=int(slow * 1000),
            steps=take_pass_steps() if name == "agent-monitor" else None,
        )

    scheduler = PeriodicScheduler(on_result=guarded(_job_result))

    def add_job(name, interval, fn, **kwargs):
        scheduler.add(name, interval, guarded(fn), **kwargs)

    def configure_job(name, interval, fn, **kwargs):
        scheduler.configure(name, interval, guarded(fn), **kwargs)

    config: BackboneConfig = current_config()

    async def _broadcast():
        # Reconcile out-of-band session churn to live Socket.IO subscribers.
        await state.feed.emit(only_if_changed=True)
        try:
            await hint_inboxes(current_config(), db, state.feed)
        except Exception:
            log.exception("Inbox hint failed (non-fatal)")

    async def _monitor():
        await store.refresh()
        refreshed = state.config
        if (
            refreshed.data_dir != current_config().data_dir
            or refreshed.database_url != current_config().database_url
        ):
            raise ValueError("Job provider context does not match")
        # Refresh is the monitor's admission read. Keep its latest roster and
        # settings, then retain that snapshot for the actual monitoring work.
        captured_config.set(refreshed)
        return await monitor_agents(current_config(), db, github, on_change=_broadcast)

    async def _retry():
        result = await delivery_retry(current_config(), db, github)
        if (
            current_config().github_intake == "webhook"
            and current_config().github.backfill_on_start
        ):
            await scheduler.retry_failed_once("github-backfill")
        return result

    async def _prune_receipts() -> int:
        receipts = await run_thread(batch_receipts, current_config().state_dir)
        if not receipts:
            return 0
        active = {str(id_) for id_ in await db.queue.active_ids()}
        return await run_thread(prune_context_receipts, receipts, active)

    async def _prune():
        days = current_config().timing.delivery_retention_days
        # Receipts first: they take their delivery times from the records pruned next.
        receipts = await db.signing.prune_receipts()
        return {
            "deliveries": await db.deliveries.prune(days),
            "events": await db.events.prune(days),
            "state_history": await db.state_history.prune(),
            # A settled batch's hook receipt must not outlive its row: queue ids
            # can be used again once old rows are gone, so receipts go before
            # the rows that free them. Receipts are listed before the active
            # rows are read, so one taken since stays.
            "context_receipts": await _prune_receipts(),
            "queue": await db.queue.prune(days),
            "diagnostics": await db.diagnostics.prune(days),
            "current_plans": await db.current_plans.prune(days),
            "signing_audit": await db.signing.prune(days),
            "signing_receipts": receipts,
            "action_log_lines": await run_thread(
                rotate_action_log, current_config().action_log_path, receipt_retention_days=days
            ),
        }

    # Immediately at startup: this is what syncs agent state after a restart.
    add_job("agent-monitor", config.timing.monitor_interval_seconds, _monitor, run_immediately=True)
    add_job("delivery-retry", config.timing.retry_interval_seconds, _retry)

    async def _transitions():
        return await run_transitions(lambda: current_config(), store, db)

    # Explicit one-time stop/restart requests (`backbone agent restart`).
    add_job("agent-transitions", _TRANSITION_TICK, _transitions)

    async def _steers():
        return await settle_steers(current_config(), db)

    # What became of each steer offer (`backbone tell --steer`).
    add_job("steer-settle", 15, _steers)
    add_job("prune", 6 * 3600, _prune)

    async def _resources():
        return await sample_resources(current_config(), db)

    # Memory and swap, recorded only under pressure (`backbone diagnostics`).
    if (sample_every := config.settings.get("diagnostics.resource_sample_seconds", 300)) > 0:
        add_job("resource-sample", sample_every, _resources)
    add_job("report-audio", 30, state.integrations.flush_report_audio, run_immediately=True)
    # Integrations re-provision their per-agent surfaces (Telegram topics):
    # a config publish triggers it immediately, this catches everything else
    # (a group discovered from a message, a transient Telegram error).
    add_job("integrations-sync", 300, state.integrations.reconcile)
    add_job("report-notifications", 30, state.integrations.flush_reports, run_immediately=True)

    async def _restart() -> None:
        # Ask uvicorn for a graceful shutdown; `backbone up` re-executes
        # itself when it sees the flag, so the same service or tmux session
        # comes back on the new code.
        shutdown = getattr(state, "request_shutdown", None)
        if shutdown is None:
            log.warning("automatic restart requires the backbone up server runner")
            return
        state.restart_requested = True
        shutdown()

    watch = UpgradeWatch(
        enabled=lambda: (
            current_config().backbone.restart_on_upgrade
            and getattr(state, "request_shutdown", None) is not None
        ),
        restart=_restart,
        in_flight=routing_in_flight,
    )
    state.upgrade_watch = watch
    add_job("upgrade-watch", 60, watch.run)

    poller = None
    if github is not None:
        poller = GitHubPoller(
            lambda: current_config(),
            db,
            github,
            issue_closed_hooks=state.issue_closed_hooks,
        )
        if config.github_intake == "poll":
            add_job(
                "github-poll", config.github.poll_interval_seconds, poller.run, run_immediately=True
            )
        elif config.github_intake == "webhook" and config.github.backfill_on_start:
            add_job("github-backfill", 0, poller.backfill, run_immediately=True, once=True)

        async def _dependencies():
            await sync_dependencies(current_config(), db, QueueSnapshot(github))

        # Sub-issue links change rarely, and a close asks GitHub for its own
        # parent. Their own job: a monitor pass never waits on these listings.
        add_job("dependency-sync", 600, _dependencies, run_immediately=True)

    sources = getattr(state, "sources", None)
    sources_poller = SourcesPoller(lambda: current_config(), db, sources) if sources else None
    if sources_poller is not None and sources.enabled:
        add_job(
            "sources-poll",
            config.sources.poll_interval_seconds,
            sources_poller.run,
            run_immediately=True,
        )

    def reconcile_jobs():
        current = state.config
        configure_job("agent-monitor", current.timing.monitor_interval_seconds, _monitor)
        configure_job("delivery-retry", current.timing.retry_interval_seconds, _retry)
        if sources_poller is not None:
            configure_job(
                "sources-poll",
                current.sources.poll_interval_seconds,
                sources_poller.run,
                enabled=bool(sources.enabled),
                run_immediately=True,
            )
        if poller is not None:
            configure_job(
                "github-poll",
                current.github.poll_interval_seconds,
                poller.run,
                enabled=current.github_intake == "poll",
                run_immediately=True,
            )

    state.reconcile_jobs = reconcile_jobs
    return scheduler


class _SocketStreams:
    """Drain socket consumers after producers stop, before their dependencies."""

    def __init__(self, sio, feed, manager):
        self.sio, self.feed, self.manager = sio, feed, manager

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        if self.sio is not None:
            await self.sio.shutdown()
        await self.feed.close()
        await self.manager.cleanup_all()

    async def health_check(self) -> dict:
        return {"healthy": self.sio is None or not self.sio._closing}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the data directory, start the database, load config, wire services."""
    from agent_backbone.api.session_updates import SessionFeed
    from agent_backbone.api.socketio_server import configure_pty_manager, get_pty_manager
    from agent_backbone.services.agents import AgentStore
    from agent_backbone.services.database import BackboneDB, check_profile_startup
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.integrations import build_integrations
    from agent_backbone.services.sources import build_sources
    from agent_backbone.services.swarm import SwarmError, teardown_for_issue

    boot: BackboneConfig = getattr(app.state, "config", None) or bootstrap_config()
    data_dir = boot.data_dir
    await check_profile_startup(boot.database_url, data_dir)
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    app.state.config = boot

    lifecycle = LifecycleManager()
    app.state.lifecycle = lifecycle

    # Stage 1: the database, then the configuration stored in it.
    app.state.db = BackboneDB(boot.database_url)
    lifecycle.register("database", app.state.db)
    await lifecycle.start_all()

    configure_pty_manager(data_dir)

    def _publish(new_config: BackboneConfig) -> None:
        from agent_backbone.base import set_log_mode

        set_log_mode(new_config.settings.get("diagnostics.mode", "standard"))
        app.state.config = new_config
        reconcile_jobs = getattr(app.state, "reconcile_jobs", None)
        if reconcile_jobs is not None:
            reconcile_jobs()
        # The set of agents may have changed: integrations re-provision their
        # per-agent surfaces (Telegram topics) against the new snapshot.
        integrations = getattr(app.state, "integrations", None)
        if integrations is not None:
            integrations.schedule_sync()

    app.state.agent_store = AgentStore(
        app.state.db,
        data_dir,
        on_change=_publish,
        credential_endpoint=(boot.backbone.host, boot.backbone.port),
    )
    lifecycle.register("agents", app.state.agent_store)
    await lifecycle.start_all()
    config: BackboneConfig = app.state.config
    # Refresh only Backbone-owned hook executables, preserving user settings.
    # Existing sessions can start recording durable usage identities immediately.
    from agent_backbone.hooks.install import install_hook_files

    try:
        await run_thread(install_hook_files, config.data_dir)
    except OSError:
        log.warning("Could not refresh Backbone hook files", exc_info=True)

    if config.github.intake == "webhook" and not config.webhook_secret:
        log.warning(
            "github.intake=webhook but GITHUB_WEBHOOK_SECRET is not set — "
            "webhooks would all be rejected, falling back to polling"
        )

    # Stage 2: everything else, against the loaded snapshot.
    app.state.github = GitHubClient(config) if config.github_ready else None
    if app.state.github is not None:
        lifecycle.register("github", app.state.github)
    app.state.feed = SessionFeed(
        lambda: app.state.config,
        getattr(app.state, "sio", None),
        db=app.state.db,
        profile_provider=getattr(app.state, "profile_provider", None),
    )
    lifecycle.register(
        "socket-streams",
        _SocketStreams(getattr(app.state, "sio", None), app.state.feed, get_pty_manager()),
    )
    # Persistent polling tasks must keep reading live authorization settings;
    # they can outlive the scheduler callback that starts them.
    app.state.integrations = build_integrations(lambda: app.state.config, db=app.state.db)
    lifecycle.register("integrations", app.state.integrations)
    lifecycle.register("api-tasks", app.state.api_tasks)
    app.state.sources = build_sources(lambda: app.state.config)

    provider_db, provider_store = app.state.db, app.state.agent_store

    # A closed issue ends the swarm that was working it (PR merged -> issue
    # closed via "Closes #N" -> teardown). Handed to ingest as a hook so
    # routing never imports the packages above it.
    async def _swarm_teardown(repo: str, issue_number: int) -> None:
        try:
            name = await teardown_for_issue(
                app.state.config, provider_db, provider_store, repo, issue_number
            )
        except SwarmError as exc:
            # A member that would not stop keeps its worktree; the swarm stays
            # active for `backbone swarm disband` once the session is dealt with.
            log.error("Swarm teardown for %s#%s failed: %s", repo, issue_number, exc)
            return
        if name:
            log.info("Swarm '%s' completed with %s#%s", name, repo, issue_number)

    app.state.issue_closed_hooks = (_swarm_teardown,)
    app.state.scheduler = _register_jobs(app)
    lifecycle.register("scheduler", app.state.scheduler)

    try:
        await lifecycle.start_all()
        log.info(
            "agent-backbone %s on http://%s:%d — data %s, %d agent(s), github=%s, "
            "integrations=%s, sources=%s",
            API_VERSION,
            config.backbone.host,
            config.backbone.port,
            data_dir,
            len(config.agents),
            config.github_intake,
            ", ".join(i.name for i in app.state.integrations.enabled) or "none",
            ", ".join(s.name for s in app.state.sources.enabled) or "none",
        )
        yield
    finally:
        app.state.api_tasks.close_admission()
        sio = getattr(app.state, "sio", None)
        if sio is not None:
            sio.close_admission()
        await lifecycle.stop_all()
        log.info("agent-backbone shutting down")


def create_app(config: BackboneConfig | None = None):
    """Build and return the ASGI application (Socket.IO wrapping FastAPI)."""
    config = config or bootstrap_config()
    if agent_credential(config.api_key):
        raise ValueError("The server requires an operator credential, not an agent credential")
    app = FastAPI(
        title="agent-backbone",
        description="Local control plane for terminal AI agents",
        version=API_VERSION,
        lifespan=lifespan,
    )
    app.state.config = config

    from agent_backbone.api.background import ApiTasks
    from agent_backbone.api.profile_binding import ProfileBindingMiddleware, profile_identity
    from agent_backbone.api.signed import SignedSenderMiddleware
    from agent_backbone.api.validation import Validations

    app.state.api_tasks = ApiTasks()
    app.state.validations = Validations(app.state.api_tasks)

    app.add_middleware(SignedSenderMiddleware)
    app.add_middleware(ProfileBindingMiddleware)
    app.add_api_route("/api/profile", profile_identity, methods=["GET"])

    cors_origins = list(config.backbone.cors_origins)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            # Sheet exports report lost views and history in this header.
            expose_headers=["X-Backbone-Sheet-Warnings"],
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
            return {
                "healthy": False,
                "components": {},
                "version": API_VERSION,
                "started": STARTED_AT,
            }
        return {**(await lifecycle.health()), "version": API_VERSION, "started": STARTED_AT}

    from agent_backbone.api.auth import require_api_key
    from agent_backbone.api.routes.agents import router as agents_router
    from agent_backbone.api.routes.config import router as config_router
    from agent_backbone.api.routes.conversations import router as conversations_router
    from agent_backbone.api.routes.current_plans import router as current_plans_router
    from agent_backbone.api.routes.deliveries import router as deliveries_router
    from agent_backbone.api.routes.diagnostics import router as diagnostics_router
    from agent_backbone.api.routes.documents import router as documents_router
    from agent_backbone.api.routes.events import router as events_router
    from agent_backbone.api.routes.fleet import router as fleet_router
    from agent_backbone.api.routes.help import router as help_router
    from agent_backbone.api.routes.integrations import router as integrations_router
    from agent_backbone.api.routes.issues import router as issues_router
    from agent_backbone.api.routes.messages import router as messages_router
    from agent_backbone.api.routes.onboarding import router as onboarding_router
    from agent_backbone.api.routes.plans import router as plans_router
    from agent_backbone.api.routes.policies import router as policies_router
    from agent_backbone.api.routes.reports import router as reports_router
    from agent_backbone.api.routes.responses import router as responses_router
    from agent_backbone.api.routes.sheets import router as sheets_router
    from agent_backbone.api.routes.signing import router as signing_router
    from agent_backbone.api.routes.skills import router as skills_router
    from agent_backbone.api.routes.status import router as status_router
    from agent_backbone.api.routes.swarms import router as swarms_router
    from agent_backbone.api.routes.usage import router as usage_router
    from agent_backbone.api.routes.webhook import router as webhook_router

    app.include_router(webhook_router)

    for router in (
        agents_router,
        status_router,  # before config_router: /api/config/agents vs /api/config/{key}
        config_router,
        deliveries_router,
        diagnostics_router,
        events_router,
        fleet_router,
        help_router,
        integrations_router,
        issues_router,
        messages_router,
        plans_router,
        onboarding_router,
        policies_router,
        reports_router,
        responses_router,
        documents_router,
        conversations_router,
        sheets_router,
        current_plans_router,
        signing_router,
        skills_router,
        swarms_router,
        usage_router,
    ):
        app.include_router(router, dependencies=[Depends(require_api_key)])

    from agent_backbone.api.socketio_server import create_sio

    sio = create_sio(cors_origins=cors_origins or [])
    sio.fastapi_app = app
    app.state.sio = sio
    from agent_backbone.api.ingress import IngressMiddleware

    return IngressMiddleware(socketio.ASGIApp(sio, app), app)
