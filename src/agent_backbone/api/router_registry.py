"""Helpers for assembling FastAPI routes."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, Depends, FastAPI, Request

from agent_backbone.base import LifecycleManager


def register_health_route(app: FastAPI) -> None:
    """Register the unauthenticated health endpoint."""

    @app.get("/health")
    async def health(request: Request):
        lifecycle: LifecycleManager = request.app.state.lifecycle
        return await lifecycle.health()


def _authenticated_routers() -> Iterable[APIRouter]:
    """Yield authenticated API routers in their existing registration order."""
    from agent_backbone.api.routes.actions import router as actions_router
    from agent_backbone.api.routes.activity import router as activity_router
    from agent_backbone.api.routes.agents import router as agents_router
    from agent_backbone.api.routes.dashboard import router as dashboard_router
    from agent_backbone.api.routes.deliveries import router as deliveries_router
    from agent_backbone.api.routes.files import router as files_router
    from agent_backbone.api.routes.heartbeats import router as heartbeats_router
    from agent_backbone.api.routes.issues import router as issues_router
    from agent_backbone.api.routes.messages import router as messages_router
    from agent_backbone.api.routes.notes import router as notes_router
    from agent_backbone.api.routes.plans import router as plans_router
    from agent_backbone.api.routes.prefect import router as prefect_router
    from agent_backbone.api.routes.repos import router as repos_router
    from agent_backbone.api.routes.rooms import router as rooms_router
    from agent_backbone.api.routes.schedule import router as schedule_router
    from agent_backbone.api.routes.status import router as status_router
    from agent_backbone.api.routes.swarms import router as swarms_router
    from agent_backbone.api.routes.workflows import router as workflows_router

    return [
        agents_router,
        dashboard_router,
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
        messages_router,
        swarms_router,
        notes_router,
        rooms_router,
        repos_router,
    ]


def register_routes(app: FastAPI) -> None:
    """Register public and authenticated routes on the app."""
    from agent_backbone.api.auth import require_api_key
    from agent_backbone.api.routes.webhook import router as webhook_router

    app.include_router(webhook_router)

    for router in _authenticated_routers():
        app.include_router(router, dependencies=[Depends(require_api_key)])
