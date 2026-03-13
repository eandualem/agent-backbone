"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_backbone.api.bootstrap import (
    attach_runtime_config,
    register_lifecycle_services,
    register_lightweight_services,
    start_runtime_services,
    stop_runtime_services,
)
from agent_backbone.api.errors import install_exception_handlers
from agent_backbone.api.router_registry import register_health_route, register_routes

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire services via LifecycleManager and manage startup/shutdown."""
    config = attach_runtime_config(app)
    await register_lifecycle_services(app)
    register_lightweight_services(app)

    try:
        await start_runtime_services(app)
        log.info("Backbone API started — port %d", config.gateway.port)
        yield
    finally:
        await stop_runtime_services(app)

        # Non-lifecycle cleanup (PTY)
        from agent_backbone.api.socketio_server import get_pty_manager

        await get_pty_manager().cleanup_all()
        log.info("Backbone API shutting down")


def create_app() -> socketio.ASGIApp:
    """Build and return the ASGI application (Socket.IO wrapping FastAPI)."""
    app = FastAPI(
        title="Agent Backbone API",
        description="REST API for agent orchestration backbone",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS for dashboard (dev mode — allow all origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_exception_handlers(app, log)
    register_health_route(app)
    register_routes(app)

    # Wrap FastAPI with Socket.IO — Socket.IO handles /socket.io/ path,
    # everything else falls through to FastAPI
    from agent_backbone.api.socketio_server import create_sio

    sio = create_sio(cors_origins=["*"])
    sio.fastapi_app = app  # Store reference for namespace config access
    app.state.sio = sio
    asgi_app = socketio.ASGIApp(sio, app)

    return asgi_app
