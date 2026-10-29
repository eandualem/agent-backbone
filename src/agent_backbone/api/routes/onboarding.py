"""Operator-only identification and finite installer actions."""

from fastapi import APIRouter, Depends

from agent_backbone.api.auth import require_operator
from agent_backbone.api.deps import get_agent_store, get_config, get_db
from agent_backbone.api.routes.reports import _ReportRoute
from agent_backbone.models import (
    SETUP_BODY_BYTES,
    SETUP_CANCEL_BODY_BYTES,
    SetupCancelFirstAgent,
    SetupFirstAgent,
)
from agent_backbone.services.routing.onboarding import cancel_first_agent, first_agent


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


@router.get("/identity", operation_id="setup_identity")
async def identity(config=Depends(get_config)):
    return {"schema_version": 1, "data_dir": str(config.data_dir.resolve())}


@router.post("/first-agent", operation_id="setup_first_agent")
async def create_first_agent(
    body: SetupFirstAgent,
    config=Depends(get_config),
    store=Depends(get_agent_store),
    db=Depends(get_db),
):
    return await first_agent(body, config, store, db)


async def cancel_first_agent_request(body: SetupCancelFirstAgent, config=Depends(get_config)):
    return await cancel_first_agent(body, config)


router.add_api_route(
    "/first-agent/cancel",
    cancel_first_agent_request,
    methods=["POST"],
    operation_id="setup_cancel_first_agent",
    route_class_override=_SetupCancelRoute,
)
