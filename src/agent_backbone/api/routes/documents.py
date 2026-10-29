"""Authenticated JSON Documents; HTML is data, never served as a web page."""

import json

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from agent_backbone.api.auth import require_api_key
from agent_backbone.api.deps import get_db
from agent_backbone.api.routes.conversations import versions
from agent_backbone.models import DOCUMENT_METADATA_BYTES, DOCUMENT_WIRE_BYTES, PublishDocument
from agent_backbone.services.database import (
    BackboneDB,
    DocumentCapacity,
    DocumentConflict,
    DocumentRateLimit,
)


class _DocumentRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            await require_api_key(request)
            if request.method == "POST":
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > DOCUMENT_WIRE_BYTES:
                        raise HTTPException(
                            413, f"document request exceeds {DOCUMENT_WIRE_BYTES} bytes"
                        )
                    raw.extend(chunk)
                request._body = bytes(raw)
            try:
                result = await handler(request)
                if "revision_id" in request.path_params and len(result.body) > DOCUMENT_WIRE_BYTES:
                    raise HTTPException(413, "document response exceeds wire limit")
                return result
            except RequestValidationError as exc:
                # Never echo HTML, huge unknown keys or arbitrary validator input.
                errors = [
                    {
                        "loc": [
                            str(v).encode("unicode_escape").decode("ascii")[:160] for v in e["loc"]
                        ],
                        "msg": e["msg"][:240],
                        "type": e["type"],
                    }
                    for e in exc.errors()[:10]
                ]
                return JSONResponse(status_code=422, content={"detail": errors})

        return bounded


router = APIRouter(prefix="/api/documents", tags=["documents"], route_class=_DocumentRoute)


def _bounded_metadata(record: dict) -> dict:
    metadata = {key: value for key, value in record.items() if key != "html"}
    # The current display name can change after publication. Keep read envelopes bounded too.
    if (
        len(json.dumps(metadata | {"html": ""}, ensure_ascii=True).encode())
        > DOCUMENT_METADATA_BYTES
    ):
        raise HTTPException(413, "document metadata exceeds byte limit")
    return record


class DocumentMetadata(BaseModel):
    id: int
    document_id: str
    author_id: str
    author_name: str
    agent_name: str | None
    request_id: str
    title: str
    created_at: str
    content_bytes: int
    content_sha256: str
    supersedes_id: int | None
    superseded_by: int | None


class DocumentDetail(DocumentMetadata):
    html: str


class DocumentPublication(BaseModel):
    record: DocumentMetadata
    created: bool


class DocumentPage(BaseModel):
    items: list[DocumentMetadata]
    has_more: bool
    next_before_id: int | None


@router.post("", response_model=DocumentPublication, operation_id="publish_document")
async def publish_document(
    publication: PublishDocument, response: Response, db: BackboneDB = Depends(get_db)
):
    try:
        record, created = await db.documents.publish(publication)
    except KeyError as exc:
        raise HTTPException(404, "author is not registered") from exc
    except DocumentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except DocumentRateLimit as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "3600"}) from exc
    except DocumentCapacity as exc:
        raise HTTPException(507, str(exc)) from exc
    response.status_code = 201 if created else 200
    return {"record": _bounded_metadata(record), "created": created}


@router.get("", response_model=DocumentPage, operation_id="list_documents")
async def documents(
    agent: str | None = Query(
        default=None, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
    ),
    author_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
    before_id: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=20, ge=1, le=100),
    db: BackboneDB = Depends(get_db),
):
    page = await db.documents.query(
        agent=agent, author_id=author_id, before_id=before_id, limit=limit
    )
    for record in page["items"]:
        _bounded_metadata(record)
    return page


@router.get("/{document_id}/revisions", operation_id="list_document_revisions")
async def document_revisions(
    document_id: str = Path(max_length=64),
    before: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
    db: BackboneDB = Depends(get_db),
):
    """Revisions newest first; read one exactly with GET /api/documents/{revision_id}."""
    return await versions(db, "document", document_id, before, limit)


@router.get("/{revision_id}", response_model=DocumentDetail, operation_id="get_document")
async def get_document(
    revision_id: int = Path(ge=1, le=2**63 - 1), db: BackboneDB = Depends(get_db)
):
    record = await db.documents.get(revision_id)
    if record is None:
        raise HTTPException(404, "document revision not found")
    return _bounded_metadata(record)
