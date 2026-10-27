"""Artifact conversations (docs/conversations.md).

The owner reads every conversation and posts owner messages with the operator
credential. An agent reads and replies only where it is responsible, was
addressed or has written, and posts as its own registration. Owner messages
are stored with their queue row, then delivered through ``safe_deliver`` by the
queue drain, so a busy agent is never interrupted.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_backbone.api.deps import get_config, get_db
from agent_backbone.models import ReportAgentName
from agent_backbone.services.database import (
    CONVERSATION_KINDS,
    BackboneDB,
    ConversationRefused,
    Limits,
    UnknownArtifact,
)
from agent_backbone.services.jobs import deliver_now
from agent_backbone.services.routing import quote_envelope_lines

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

_PLANNED = ("sheet", "plan", "skill", "policy")
_MAX_ID = 2**63 - 1


def _plain(value: str) -> str:
    if any(
        (unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} and c not in "\n\t") or c in "‪‫‬‭‮⁦⁧⁨⁩"
        for c in value
    ):
        raise ValueError("use plain text without control characters")
    return value


class SheetAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tab_id: str = Field(min_length=1, max_length=64)
    row_ids: list[str] = Field(default=[], max_length=200)
    column_ids: list[str] = Field(default=[], max_length=200)


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision_id: str | None = Field(default=None, min_length=1, max_length=64)
    sheet: SheetAnchor | None = None
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    label: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("label")
    @classmethod
    def one_line(cls, value: str | None) -> str | None:
        return value if value is None else _plain(" ".join(value.split()))


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=64_000)
    author: ReportAgentName | None = None
    recipient: str | None = Field(default=None, min_length=1, max_length=128)
    anchor: Anchor | None = None
    in_reply_to: int | None = Field(default=None, ge=1, le=_MAX_ID)
    resulting_revision_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("text")
    @classmethod
    def readable(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a message cannot be blank")
        return _plain(value.strip())

    @model_validator(mode="after")
    def roles(self) -> MessageBody:
        if self.author is None and (self.in_reply_to or self.resulting_revision_id):
            raise ValueError("in_reply_to and resulting_revision_id are for agent replies")
        if self.author is not None and self.recipient is not None:
            raise ValueError("recipient is for owner messages")
        return self


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    read_through: int = Field(ge=0, le=_MAX_ID)


class RedeliverBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


def compose(entry: dict, artifact: dict) -> str:
    """The summary pasted into the recipient's terminal: not the whole thread."""
    anchor = entry["anchor"] or {}
    version = next(
        (v for v in artifact["versions"] if v["revision_id"] == anchor.get("revision_id")),
        artifact["latest"],
    )
    where = ", ".join(part for part in (version["label"], anchor.get("label")) if part)
    kind, artifact_id = artifact["kind"], artifact["artifact_id"]
    return (
        f"[via:backbone conversation:{kind}/{artifact_id} from:owner]"
        f' On "{artifact["title"]}" ({where}):\n'
        f"{quote_envelope_lines(entry['text'])}\n"
        f'Reply: backbone conversations reply {kind} {artifact_id} "…" --to {entry["id"]}'
    )


def _refused(exc: ConversationRefused) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"result": "refused", "error": exc.error, "message": exc.message},
    )


def _unknown(exc: UnknownArtifact) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "unknown_artifact", "message": str(exc)})


def _kind(kind: str) -> None:
    if kind not in CONVERSATION_KINDS:
        known = kind in _PLANNED
        raise UnknownArtifact(
            f"Conversations on {kind}s are not available yet." if known else f"No kind {kind}."
        )


def _owner(request: Request) -> None:
    if request.state.credential_role != "operator":
        raise HTTPException(403, "This operation requires an operator credential")


def _reader(request: Request, agent: str | None) -> str | None:
    """None for the owner; otherwise the agent asking, which must say who it is."""
    if request.state.credential_role == "operator":
        return None
    name = request.headers.get("X-Backbone-Client-Agent") or agent
    if not name:
        raise HTTPException(403, "An agent credential reads conversations as a named agent")
    return name


def _cursor(value: str | None, prefix: str) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(rf"{prefix}?([0-9]{{1,19}})", value)
    if not match or int(match[1]) > _MAX_ID:
        raise HTTPException(422, f"invalid cursor {value[:40]!r}")
    return int(match[1])


def _since(value: str | None) -> tuple[int, int | None] | None:
    """A summaries ``cursor``: ``e<entry ID>[.<µs since the epoch>]``."""
    if value is None:
        return None
    match = re.fullmatch(r"e([0-9]{1,19})(?:\.([0-9]{1,19}))?", value)
    if not match or int(match[1]) > _MAX_ID:
        raise HTTPException(422, f"invalid cursor {value[:40]!r}")
    return int(match[1]), int(match[2]) if match[2] else None


def _limits(config) -> Limits:
    return Limits(
        max_messages=config.settings.get("conversations.max_messages", 10_000),
        owner_messages_per_hour=config.settings.get("conversations.owner_messages_per_hour", 60),
    )


@router.get("", operation_id="list_conversations")
async def conversations(
    request: Request,
    kind: str | None = Query(default=None, max_length=32),
    ids: str | None = Query(default=None, max_length=4096),
    after: str | None = Query(default=None, max_length=40),
    since: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    agent: str | None = Query(default=None, max_length=128),
    db: BackboneDB = Depends(get_db),
):
    """Conversation summaries for badges, most recently active first."""
    keys = []
    for key in (ids or "").split(","):
        if key:
            key_kind, _, artifact_id = key.partition(":")
            if not artifact_id:
                raise HTTPException(422, "ids are kind:artifact_id keys")
            keys.append((key_kind, artifact_id))
    if len(keys) > 50:
        raise HTTPException(422, "at most 50 ids")
    return await db.conversations.summaries(
        kind=kind,
        keys=keys,
        reader=_reader(request, agent),
        after=_cursor(after, "c"),
        since=_since(since),
        limit=limit,
    )


@router.get("/{kind}/{artifact_id}", operation_id="get_conversation")
async def conversation(
    kind: str,
    artifact_id: str,
    request: Request,
    before: str | None = Query(default=None, max_length=40),
    after: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    entries: str | None = Query(default=None, max_length=1024),
    agent: str | None = Query(default=None, max_length=128),
    db: BackboneDB = Depends(get_db),
):
    """The header and a page of entries; an unchanged poll returns 304.
    ``entries=ID,…`` (up to 50) returns those entries instead, to refresh their
    delivery state; an ID that is not in this conversation is left out."""
    ids = None
    if entries is not None:
        ids = [_cursor(value, "e") for value in entries.split(",") if value]
        if not 1 <= len(ids) <= 50:
            raise HTTPException(422, "entries takes 1 to 50 entry IDs")
    try:
        _kind(kind)
        page = await db.conversations.conversation(
            kind,
            artifact_id,
            reader=_reader(request, agent),
            before=_cursor(before, "e"),
            after=_cursor(after, "e"),
            limit=limit,
            ids=ids,
        )
    except UnknownArtifact as exc:
        return _unknown(exc)
    except ConversationRefused as exc:
        return JSONResponse(
            status_code=exc.status, content={"error": exc.error, "message": exc.message}
        )
    body = json.dumps(page, separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="application/json", headers={"ETag": etag})


@router.post("/{kind}/{artifact_id}/messages", operation_id="post_conversation_message")
async def post_message(
    kind: str,
    artifact_id: str,
    body: MessageBody,
    request: Request,
    background: BackgroundTasks,
    config=Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Append a message. Owner messages are delivered to their recipient."""
    if body.author is None:
        _owner(request)
    try:
        _kind(kind)
        entry, created, target = await db.conversations.post(
            kind,
            artifact_id,
            author=body.author,
            request_id=body.request_id,
            text=body.text,
            recipient=body.recipient,
            anchor=body.anchor.model_dump(exclude_none=True) if body.anchor else None,
            in_reply_to=body.in_reply_to,
            resulting_revision_id=body.resulting_revision_id,
            limits=_limits(config),
            compose=compose,
        )
    except UnknownArtifact as exc:
        return _unknown(exc)
    except ConversationRefused as exc:
        return _refused(exc)
    except KeyError as exc:
        raise HTTPException(404, "author is not registered") from exc
    if target:
        background.add_task(deliver_now, config, db, request.app.state.github, target)
    return JSONResponse(
        status_code=201 if created else 200, content={"result": "accepted", "entry": entry}
    )


@router.post("/{kind}/{artifact_id}/read", operation_id="read_conversation")
async def read(
    kind: str,
    artifact_id: str,
    body: ReadBody,
    request: Request,
    db: BackboneDB = Depends(get_db),
):
    """Advance the owner's read marker; it never moves backwards."""
    _owner(request)
    try:
        _kind(kind)
        return await db.conversations.mark_read(kind, artifact_id, body.read_through)
    except UnknownArtifact as exc:
        return _unknown(exc)


@router.post(
    "/{kind}/{artifact_id}/messages/{entry_id}/redeliver",
    operation_id="redeliver_conversation_message",
)
async def redeliver(
    kind: str,
    artifact_id: str,
    entry_id: int,
    body: RedeliverBody,
    request: Request,
    background: BackgroundTasks,
    config=Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Queue a new delivery for a failed owner message; no new entry."""
    _owner(request)
    try:
        _kind(kind)
        result, error, entry, target = await db.conversations.redeliver(
            kind, artifact_id, entry_id, body.request_id, compose
        )
    except UnknownArtifact as exc:
        return _unknown(exc)
    except ConversationRefused as exc:
        return _refused(exc)
    if target:
        background.add_task(deliver_now, config, db, request.app.state.github, target)
    return JSONResponse(
        status_code=409 if result == "refused" else 200,
        content={"result": result, "entry": entry, "error": error},
    )


async def versions(db: BackboneDB, kind: str, artifact_id: str, before, limit):
    """A kind's version list, shared by its own router."""
    try:
        return await db.conversations.versions(kind, artifact_id, before=before, limit=limit)
    except UnknownArtifact as exc:
        return _unknown(exc)
