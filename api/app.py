"""FastAPI application factory.

Creates the app with lifespan management, CORS, and router registration.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import BackboneConfig

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config and warm dedup cache on startup."""
    config = BackboneConfig.from_toml()
    app.state.config = config

    # Load dedup state from SQLite for cold-restart recovery
    from gateway.server import _load_dedup_from_db

    await _load_dedup_from_db(str(config.delivery.db_file), config.max_delivery_ids)

    log.info("Backbone API started — port %d", config.gateway_port)
    yield
    log.info("Backbone API shutting down")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
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

    # Health check (unauthenticated)
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # Register routers
    from api.auth import require_api_key
    from api.routes.webhook import router as webhook_router

    app.include_router(webhook_router)

    # API routers (authenticated) — imported lazily to avoid circular imports
    from fastapi import Depends

    from api.routes.actions import router as actions_router
    from api.routes.agents import router as agents_router
    from api.routes.deliveries import router as deliveries_router
    from api.routes.files import router as files_router
    from api.routes.heartbeats import router as heartbeats_router
    from api.routes.issues import router as issues_router
    from api.routes.plans import router as plans_router
    from api.routes.prefect import router as prefect_router
    from api.routes.status import router as status_router
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
    ]
    for r in api_routers:
        app.include_router(r, dependencies=[Depends(require_api_key)])

    return app
