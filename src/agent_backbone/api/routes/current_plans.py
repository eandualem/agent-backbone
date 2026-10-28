"""Current authored checklists; distinct from native runtime approval plans."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from agent_backbone.api.auth import require_operator
from agent_backbone.api.deps import get_config, get_db
from agent_backbone.api.models import MessageResponse
from agent_backbone.api.routes.conversations import versions
from agent_backbone.api.routes.reports import _ReportRoute
from agent_backbone.models import CURRENT_PLAN_BODY_BYTES, CurrentPlanFeedback, PublishCurrentPlan
from agent_backbone.services.database import CurrentPlanError
from agent_backbone.services.jobs import deliver_now
from agent_backbone.services.routing import envelope


class _CurrentPlanRoute(_ReportRoute):
    body_limit = CURRENT_PLAN_BODY_BYTES
    label = "current plan"

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            try:
                return await handler(request)
            except CurrentPlanError as exc:
                return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

        return bounded


router = APIRouter(
    prefix="/api/current-plans", tags=["current-plans"], route_class=_CurrentPlanRoute
)
Identity = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


@router.get("/schema", operation_id="current_plan_schema")
async def schema():
    return {
        "publication": PublishCurrentPlan.model_json_schema(),
        "feedback": CurrentPlanFeedback.model_json_schema(),
        "request_bytes": CURRENT_PLAN_BODY_BYTES,
    }


@router.post("", operation_id="publish_current_plan")
async def publish(body: PublishCurrentPlan, response: Response, db=Depends(get_db)):
    result = await db.current_plans.publish(body)
    response.status_code = 201 if result["created"] else 200
    return result


@router.get("", operation_id="list_current_plans")
async def listing(
    agent: Annotated[
        str | None, Query(max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after_author_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    db=Depends(get_db),
):
    return await db.current_plans.query(agent=agent, limit=limit, after_author_id=after_author_id)


@router.get("/{author_id}", operation_id="get_current_plan")
async def get(author_id: Identity, db=Depends(get_db)):
    return await db.current_plans.get(author_id)


@router.get("/{author_id}/versions", operation_id="list_current_plan_versions")
async def plan_versions(
    author_id: Identity,
    before: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
    db=Depends(get_db),
):
    return await versions(db, "plan", author_id, before, limit)


@router.get("/{author_id}/versions/{revision_id}", operation_id="get_current_plan_version")
async def plan_version(author_id: Identity, revision_id: Identity, db=Depends(get_db)):
    return await db.current_plans.version(author_id, revision_id)


@router.post("/{author_id}/feedback", operation_id="current_plan_feedback")
async def feedback(
    author_id: Identity,
    body: CurrentPlanFeedback,
    request: Request,
    db=Depends(get_db),
    config=Depends(get_config),
):
    if body.author is None:
        await require_operator(request)
    signed = getattr(request.state, "signed_sender", None)
    receipt = await db.current_plans.feedback(
        author_id, body, lambda sender, text: envelope(sender, text, relay=signed is not None)
    )
    target = await db.current_plans.recipient(author_id)
    if target is not None:
        await deliver_now(config, db, getattr(request.app.state, "github", None), target)
    state = await db.queue.by_operation(receipt["operation_id"])
    status = state["status"] if state else "unknown"
    queued = status in {"pending", "in_progress", "checkpoint", "uncertain"}
    outcome = "queued" if queued and status != "uncertain" else status
    delivery = MessageResponse(
        ok=status == "delivered",
        session=target or "",
        outcome=outcome,
        queued=queued,
        queue="stored" if queued else None,
        detail="Feedback delivery: " + outcome + ". This is not an agent answer.",
        operation_id=receipt["operation_id"],
        queue_id=state["id"] if state else None,
    )
    return receipt | {"delivery": delivery.model_dump()}
