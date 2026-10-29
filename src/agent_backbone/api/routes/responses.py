"""Explicit answers, separate from authored progress reports."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel

from agent_backbone.api.deps import get_db
from agent_backbone.api.routes.conversations import versions
from agent_backbone.api.routes.reports import _ReportRoute
from agent_backbone.models import RESPONSE_BODY_BYTES, AgentResponse, PublishResponse
from agent_backbone.services.database import (
    BackboneDB,
    ReportConflict,
    ReportForbidden,
    ReportRateLimit,
)


class _ResponseRoute(_ReportRoute):
    body_limit = RESPONSE_BODY_BYTES
    label = "response"


router = APIRouter(prefix="/api/responses", tags=["responses"], route_class=_ResponseRoute)


class ResponseRecord(BaseModel):
    id: int
    author_id: str
    author_name: str
    agent_name: str | None
    request_id: str
    created_at: str
    source: str
    response: AgentResponse
    supersedes_id: int | None
    superseded_by: int | None = None
    conversation_id: int
    age_seconds: int
    telegram_delivery: str
    telegram_audio_delivery: str


class ResponsePublication(BaseModel):
    record: ResponseRecord
    created: bool


class ResponsePage(BaseModel):
    items: list[ResponseRecord]
    has_more: bool
    next_before_id: int | None


@router.get("/schema", operation_id="response_schema")
async def schema():
    return {
        "response_schema": AgentResponse.model_json_schema(),
        "publication_schema": PublishResponse.model_json_schema(),
        "request_bytes": RESPONSE_BODY_BYTES,
    }


@router.post("", response_model=ResponsePublication, operation_id="publish_response")
async def publish_response(
    publication: PublishResponse, response: Response, db: BackboneDB = Depends(get_db)
):
    """The trusted local client supplies the author, question and correction link."""
    try:
        record, created = await db.reports.publish(publication)
    except KeyError as exc:
        raise HTTPException(404, "agent is not registered") from exc
    except ReportForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except ReportConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ReportRateLimit as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "3600"}) from exc
    response.status_code = 201 if created else 200
    return {"record": record, "created": created}


@router.get("", response_model=ResponsePage, operation_id="list_responses")
async def responses(
    agent: str | None = Query(
        default=None, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
    ),
    before_id: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=20, ge=1, le=100),
    db: BackboneDB = Depends(get_db),
):
    return await db.reports.responses(agent=agent, before_id=before_id, limit=limit)


@router.get("/{conversation_id}/revisions", operation_id="list_response_revisions")
async def response_revisions(
    conversation_id: str = Path(max_length=20),
    before: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
    db: BackboneDB = Depends(get_db),
):
    """A correction chain newest first; read one exactly with GET /api/responses/{id}."""
    return await versions(db, "response", conversation_id, before, limit)


@router.get("/{response_id}", response_model=ResponseRecord, operation_id="get_response")
async def get_response(
    response_id: int = Path(ge=1, le=2**63 - 1), db: BackboneDB = Depends(get_db)
):
    if record := await db.reports.get(response_id, kind="response"):
        return record
    raise HTTPException(404, "response not found; it may have been pruned")
