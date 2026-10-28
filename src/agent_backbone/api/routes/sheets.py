"""Authenticated retained Sheets: calculated revisions, bounded reads and feedback."""

import base64
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from agent_backbone.api.auth import require_api_key, require_operator
from agent_backbone.api.deps import get_config, get_db
from agent_backbone.api.models import MessageResponse
from agent_backbone.services.jobs import deliver_now
from agent_backbone.services.routing import envelope
from agent_backbone.sheets import (
    SHEET_METADATA_BYTES,
    SHEET_RANGE_BYTES,
    SHEET_WIRE_BYTES,
    CreateSheet,
    EditSheet,
    SheetError,
    SheetFeedback,
    SheetImport,
)

ID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Revision = Annotated[str, Query(pattern=r"^[0-9a-f]{32}$")]
Cursor = Annotated[str | None, Query(max_length=512)]
Limit = Annotated[int, Query(ge=1, le=100)]


class _SheetRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            await require_api_key(request)
            try:
                response = await handler(request)
                limit = (
                    SHEET_RANGE_BYTES
                    if request.url.path.endswith("/range")
                    else SHEET_WIRE_BYTES
                    if request.url.path.endswith("/export")
                    else SHEET_METADATA_BYTES
                )
                if len(response.body) > limit:
                    code = (
                        "range_too_large"
                        if request.url.path.endswith("/range")
                        else "response_too_large"
                    )
                    raise SheetError(code, "request a smaller page or range", 413)
                return response
            except SheetError as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status)
            except RequestValidationError as exc:
                errors = [
                    {
                        "loc": [str(v).encode("unicode_escape").decode()[:160] for v in e["loc"]],
                        "type": e["type"],
                        "msg": e["msg"][:240],
                    }
                    for e in exc.errors()[:10]
                ]
                return JSONResponse({"detail": errors}, status_code=422)

        return bounded


router = APIRouter(prefix="/api/sheets", tags=["sheets"], route_class=_SheetRoute)


@router.post("", operation_id="create_sheet")
async def create_sheet(body: CreateSheet, response: Response, db=Depends(get_db)):
    result = await db.sheets.mutate(body)
    response.status_code = 201 if result["created"] else 200
    return {key: value for key, value in result.items() if key != "warnings"}


@router.post("/import", operation_id="import_sheet")
async def import_sheet(body: SheetImport, response: Response, db=Depends(get_db)):
    result = await db.sheets.mutate(body, importing=True)
    response.status_code = 201 if result["created"] else 200
    return result


@router.get("", operation_id="list_sheets")
async def list_sheets(
    author: Annotated[str | None, Query(max_length=128)] = None,
    author_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
    db=Depends(get_db),
):
    return await db.sheets.query(author=author, author_id=author_id, cursor=cursor, limit=limit)


@router.get("/{sheet_id}", operation_id="get_sheet")
async def get_sheet(
    sheet_id: ID,
    revision_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    db=Depends(get_db),
):
    return await db.sheets.get(sheet_id, revision_id)


@router.get("/{sheet_id}/revisions", operation_id="sheet_history")
async def sheet_history(sheet_id: ID, cursor: Cursor = None, limit: Limit = 50, db=Depends(get_db)):
    return await db.sheets.query(sheet_id=sheet_id, cursor=cursor, limit=limit)


@router.post("/{sheet_id}/revisions", operation_id="edit_sheet")
async def edit_sheet(
    sheet_id: ID, body: EditSheet, request: Request, response: Response, db=Depends(get_db)
):
    if body.author is None:
        await require_operator(request)
    result = await db.sheets.mutate(body, sheet_id=sheet_id)
    response.status_code = 201 if result["created"] else 200
    return {key: value for key, value in result.items() if key != "warnings"}


@router.get("/{sheet_id}/tabs/{tab_id}/range", operation_id="sheet_range")
async def get_range(
    sheet_id: ID,
    tab_id: ID,
    revision_id: Revision,
    row_offset: Annotated[int, Query(ge=0)] = 0,
    row_limit: Annotated[int, Query(ge=1, le=200)] = 100,
    column_offset: Annotated[int, Query(ge=0)] = 0,
    column_limit: Annotated[int, Query(ge=1, le=100)] = 20,
    order: Literal["canonical", "view"] = "canonical",
    db=Depends(get_db),
):
    return await db.sheets.range(
        sheet_id,
        revision_id,
        tab_id,
        row_offset=row_offset,
        row_limit=row_limit,
        column_offset=column_offset,
        column_limit=column_limit,
        order=order,
    )


@router.get("/{sheet_id}/export", operation_id="export_sheet")
async def export_sheet(
    sheet_id: ID,
    revision_id: Revision,
    format: Literal["json", "csv", "tsv", "xlsx"],
    tab_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    db=Depends(get_db),
):
    if format in {"csv", "tsv"} and tab_id is None:
        raise HTTPException(422, "CSV/TSV export requires tab_id")
    result = await db.sheets.export(sheet_id, revision_id, format, tab_id)
    return Response(
        base64.b64decode(result["content"], validate=True),
        media_type=result["mime"],
        headers={
            "Content-Disposition": f'attachment; filename="{result["filename"]}"',
            "X-Content-Type-Options": "nosniff",
            "X-Backbone-Sheet-Warnings": json.dumps(result["warnings"], ensure_ascii=True),
        },
    )


@router.post("/{sheet_id}/feedback", operation_id="sheet_feedback")
async def sheet_feedback(
    sheet_id: ID,
    body: SheetFeedback,
    request: Request,
    db=Depends(get_db),
    config=Depends(get_config),
):
    if body.author is None:
        await require_operator(request)
    signed = getattr(request.state, "signed_sender", None)
    row = await db.sheets.feedback(
        sheet_id, body, lambda sender, message: envelope(sender, message, relay=signed is not None)
    )
    target = await db.sheets.recipient(row["recipient_id"])
    if target is not None:
        await deliver_now(config, db, getattr(request.app.state, "github", None), target)
    state = await db.queue.by_operation(row["operation_id"])
    status = state["status"] if state else "unknown"
    delivered = status == "delivered"
    queued = status in {"pending", "in_progress", "checkpoint", "uncertain"}
    outcome = "queued" if queued and status != "uncertain" else status
    receipt = MessageResponse(
        ok=delivered,
        session=target or "",
        outcome=outcome,
        queued=queued,
        queue="stored" if queued else None,
        detail="Feedback delivery: " + outcome + ". This does not mean the agent has answered.",
        operation_id=row["operation_id"],
        queue_id=state["id"] if state else None,
    )
    return {
        "feedback_id": row["feedback_id"],
        "sheet_id": sheet_id,
        "observed_revision_id": row["observed_revision_id"],
        "operation_id": row["operation_id"],
        "delivery": receipt.model_dump(),
    }
