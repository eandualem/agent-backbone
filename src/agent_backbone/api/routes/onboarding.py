"""Operator-only identification and finite installer actions."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_backbone.api.auth import require_operator
from agent_backbone.api.deps import get_agent_store, get_config, get_db
from agent_backbone.api.profile_binding import BINDING_MISMATCH, provider_context_current
from agent_backbone.api.routes.reports import _ReportRoute
from agent_backbone.models import (
    SETUP_BODY_BYTES,
    SETUP_CANCEL_BODY_BYTES,
    BoundSetupCancelFirstAgent,
    BoundSetupFirstAgent,
    SetupCancelFirstAgent,
    SetupFirstAgent,
)
from agent_backbone.services.database import SetupReceiptError
from agent_backbone.services.routing.onboarding import cancel_first_agent, first_agent
from agent_backbone.services.runtimes import RUNTIMES, NativeEnvironment


class _SetupRoute(_ReportRoute):
    body_limit = SETUP_BODY_BYTES
    label = "setup"


class _SetupCancelRoute(_SetupRoute):
    body_limit = SETUP_CANCEL_BODY_BYTES


router = APIRouter(
    prefix="/api/setup",
    tags=["setup"],
    dependencies=[Depends(require_operator)],
    route_class=_SetupRoute,
)


def _isolated_provider(request: Request) -> bool:
    """Schema-1 setup is for the Default profile."""
    provider = getattr(request.state, "profile_provider", None)
    return provider is not None and provider.reservation.kind != "legacy_default"


@router.get("/identity", operation_id="setup_identity")
async def identity(config=Depends(get_config)):
    return {"schema_version": 1, "data_dir": str(config.data_dir.resolve())}


@router.post("/first-agent", operation_id="setup_first_agent")
async def create_first_agent(
    request: Request,
    body: SetupFirstAgent | BoundSetupFirstAgent,
    config=Depends(get_config),
    store=Depends(get_agent_store),
    db=Depends(get_db),
):
    if isinstance(body, BoundSetupFirstAgent):
        return await _bound_setup(request, body, config, db, store)
    if _isolated_provider(request):
        raise HTTPException(status_code=409, detail=BINDING_MISMATCH)
    return await first_agent(body, config, store, db)


async def cancel_first_agent_request(
    request: Request,
    body: SetupCancelFirstAgent | BoundSetupCancelFirstAgent,
    config=Depends(get_config),
    db=Depends(get_db),
):
    if isinstance(body, BoundSetupFirstAgent):
        return await _bound_setup(request, body, config, db)
    if _isolated_provider(request):
        raise HTTPException(status_code=409, detail=BINDING_MISMATCH)
    return await cancel_first_agent(body, config)


async def _bound_setup(request, body, config, db, store=None):
    """Keep the captured request and provider alive after client cancellation."""
    provider = getattr(request.state, "profile_provider", None)
    if provider is None:
        raise HTTPException(status_code=409, detail=BINDING_MISMATCH)
    body = body.model_copy(deep=True)
    identity = provider.reservation.identity
    if (body.profile_id, body.expected_installation_id, body.expected_lifecycle_id) != (
        identity.profile_id,
        identity.installation_id,
        identity.lifecycle_id,
    ):
        raise HTTPException(status_code=409, detail=BINDING_MISMATCH)
    state = request.app.state
    environments = {}

    def owner_matches():
        return not (
            getattr(state, "profile_provider", None) is not provider
            or state.db is not db
            or state.config.data_dir != config.data_dir
            or state.config.database_url != config.database_url
            or (
                store is not None
                and (
                    state.agent_store is not store
                    or not store.owns_storage(db, config.data_dir)
                    or store.config.data_dir != config.data_dir
                    or store.config.database_url != config.database_url
                )
            )
        )

    async def check_provider():
        if (
            not owner_matches()
            or not await provider_context_current(provider, state.config, db)
            or not owner_matches()
        ):
            raise SetupReceiptError("binding_mismatch", "Profile binding does not match.")

    async def check_effect():
        await check_provider()
        if store is not None:
            for name in body.hooks:
                runtime = RUNTIMES.get(name)
                try:
                    env = runtime.environment() if runtime is not None else None
                    if (
                        not isinstance(env, NativeEnvironment)
                        or not env.root.is_relative_to(provider.reservation.data_dir)
                        or env != environments.setdefault(name, env)
                    ):
                        raise ValueError("Native context is unavailable")
                except ValueError:
                    raise SetupReceiptError(
                        "runtime_permission", "The runtime has no owned native environment."
                    ) from None

    async def run():
        if isinstance(body, BoundSetupCancelFirstAgent):
            return await cancel_first_agent(body, config, check_effect=check_provider)
        return await first_agent(
            body, config, store, db, check_effect=check_effect, check_admission=check_provider
        )

    task = state.api_tasks.create(run, config=config, db=db, provider=provider)
    return await asyncio.shield(task)


router.add_api_route(
    "/first-agent/cancel",
    cancel_first_agent_request,
    methods=["POST"],
    operation_id="setup_cancel_first_agent",
    route_class_override=_SetupCancelRoute,
)
