"""Agent-authored progress tools: strict publication and read-only feed/history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from agent_backbone.api.auth import require_api_key
from agent_backbone.api.deps import get_db
from agent_backbone.models import (
    REPORT_BODY_BYTES,
    REPORT_LINKS,
    REPORT_TEXT_CHARACTERS,
    REPORTS_PER_HOUR,
    ProgressReport,
    PublishReport,
    ReportQuery,
    report_error_details,
    report_example,
)
from agent_backbone.services.database import (
    BackboneDB,
    ReportConflict,
    ReportForbidden,
    ReportRateLimit,
)


def _validation_errors(exc: ValidationError | RequestValidationError) -> list[dict]:
    # Pydantic's default response repeats rejected input. A giant paragraph must
    # produce a short actionable error, not another giant paragraph.
    return report_error_details(exc.errors())


class _ReportRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            await require_api_key(request)
            if request.method == "POST":
                data = bytearray()
                async for chunk in request.stream():
                    if len(data) + len(chunk) > REPORT_BODY_BYTES:
                        raise HTTPException(
                            413, f"report request exceeds {REPORT_BODY_BYTES} UTF-8 bytes"
                        )
                    data.extend(chunk)
                request._body = bytes(data)
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return JSONResponse(status_code=422, content={"detail": _validation_errors(exc)})

        return bounded


router = APIRouter(prefix="/api/reports", tags=["reports"], route_class=_ReportRoute)


class ReportRecord(BaseModel):
    id: int
    author_id: str
    author_name: str
    agent_name: str | None
    request_id: str
    created_at: str
    source: str
    report: ProgressReport
    age_seconds: int
    stale: bool
    telegram_delivery: str = "not_requested"
    telegram_audio_delivery: str = "not_requested"


class PublicationResult(BaseModel):
    record: ReportRecord
    created: bool


class ReportEntry(BaseModel):
    agent_name: str | None
    author_id: str | None
    record: ReportRecord | None


class ReportPage(BaseModel):
    items: list[ReportEntry]
    history: bool
    generated_at: str
    stale_after_seconds: int
    has_more: bool
    next_cursor: str | None


@router.get("/schema", operation_id="progress_report_schema")
async def schema():
    """Retrieve required sections, character/link limits and a synthetic example."""
    return {
        "report_schema": ProgressReport.model_json_schema(),
        "publication_schema": PublishReport.model_json_schema(),
        "limits": {
            "request_bytes": REPORT_BODY_BYTES,
            "text_and_title_characters": REPORT_TEXT_CHARACTERS,
            "links": REPORT_LINKS,
            "reports_per_agent_per_hour": REPORTS_PER_HOUR,
        },
        "example": report_example(),
    }


@router.post("", response_model=PublicationResult, operation_id="publish_progress_report")
async def publish_report(
    publication: PublishReport, response: Response, db: BackboneDB = Depends(get_db)
):
    """Publish a short report for a known agent. Reuse request_id on retries.

    The authenticated local client supplies the agent identity; this is not a
    per-agent authentication boundary. Publication queues a durable Telegram
    notification; the integration delivers it to the configured agents group.
    """
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


@router.get("", response_model=ReportPage, operation_id="list_progress_reports")
async def reports(
    agent: list[str] = Query(default=[], max_length=20),
    author_id: str | None = Query(default=None),
    history: bool = False,
    members: bool = False,
    limit: int = Query(default=5, ge=1, le=20),
    cursor: str | None = Query(default=None, max_length=1024),
    db: BackboneDB = Depends(get_db),
):
    """Latest per registered agent, or retained history; reads never prompt agents."""
    try:
        query = ReportQuery(
            agents=agent,
            author_id=author_id,
            history=history,
            members=members,
            limit=limit,
            cursor=cursor,
        )
        return await db.reports.query(query)
    except ValidationError as exc:
        raise HTTPException(422, _validation_errors(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{report_id}", response_model=ReportRecord, operation_id="get_progress_report")
async def get_report(report_id: int = Path(ge=1, le=2**63 - 1), db: BackboneDB = Depends(get_db)):
    if record := await db.reports.get(report_id):
        return record
    raise HTTPException(404, "report not found; it may have been pruned")
