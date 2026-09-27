"""Signed senders at the API boundary (docs/owner-confirmed-messages.md).

A request is *made as* a name when its route's sender field names it (the
table below; a name that is only the subject of a request doesn't count).
For a name with an enrolled key, every such request must carry a valid
signature; while an enrollment is pending, requests are checked against the
pending key and the outcome recorded, without changing what is admitted. A
name with neither is handled exactly as before, signature headers ignored.

Refusals return ``{"detail": {"reason", "message"}}`` and leave a
metadata-only audit row, never a body.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Match

from agent_backbone import signing
from agent_backbone.api.auth import api_key_valid

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_048_576
HEADERS = (
    "x-backbone-sender",
    "x-backbone-key-epoch",
    "x-backbone-timestamp",
    "x-backbone-nonce",
    "x-backbone-audience",
    "x-backbone-signature",
)


@dataclass(frozen=True)
class SenderRoute:
    method: str
    template: str  # the router's own path template
    where: str  # "body", "path" or "header"
    field: str
    purpose: str = "request"


SENDER_ROUTES: tuple[SenderRoute, ...] = (
    SenderRoute("POST", "/api/messages", "body", "from_entity"),
    SenderRoute("POST", "/api/steer", "body", "from_entity"),
    SenderRoute("POST", "/api/messages/inbox", "body", "session"),
    SenderRoute("POST", "/api/agents/{name}/approve", "body", "from_entity"),
    SenderRoute("POST", "/api/agents/{name}/deny", "body", "from_entity"),
    SenderRoute("POST", "/api/agents/{session}/restart", "body", "from_entity"),
    SenderRoute("POST", "/api/integrations/reply", "body", "session"),
    SenderRoute("POST", "/api/reports", "body", "agent"),
    SenderRoute("POST", "/api/swarms", "body", "initiator"),
    SenderRoute("POST", "/api/agents/{session}/state", "path", "session"),
    SenderRoute("POST", "/api/skills", "body", "actor"),
    SenderRoute("PUT", "/api/skills/{name}/tags", "body", "actor"),
    SenderRoute("POST", "/api/signing/rotation", "header", "", "rotate"),
    SenderRoute("GET", "/api/signing/receipts", "header", ""),
)
# A signed body on these routes is exactly this shape: an unknown field is refused.
SIGNED_SHAPES: dict[str, frozenset[str]] = {
    "/api/messages": frozenset(
        {"target_session", "from_entity", "message", "priority", "owner_confirmation"}
    ),
    "/api/steer": frozenset({"target_session", "from_entity", "message", "owner_confirmation"}),
    "/api/messages/inbox": frozenset({"session", "acknowledge"}),
    "/api/agents/{name}/approve": frozenset({"from_entity"}),
    "/api/agents/{name}/deny": frozenset({"from_entity"}),
}
# Routes whose confirmed requests commit their nonce with the receipt, in one transaction.
CONFIRMABLE = frozenset({"/api/messages", "/api/steer"})
_BY_ROUTE = {(r.method, r.template): r for r in SENDER_ROUTES}


@dataclass(frozen=True)
class SignedSender:
    """A verified request, handed to the route as ``request.state.signed_sender``."""

    sender: str
    sender_key: str
    epoch: int
    nonce: str
    timestamp: int
    request_hash: str = ""
    nonce_in_route: bool = False
    """The route commits the nonce itself (a confirmed message or steer)."""


class Refusal(Exception):
    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message

    def as_http(self):
        from fastapi import HTTPException

        return HTTPException(self.status, {"reason": self.reason, "message": self.message})

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={"detail": {"reason": self.reason, "message": self.message}},
        )


def now() -> int:
    return int(time.time())


def match(scope) -> tuple[SenderRoute, dict] | None:
    """The sender route a request reaches, asked of the router itself, so
    whatever path the router accepts (a mounted prefix, a trailing newline
    its pattern allows) is the path checked here. Returns the route and its
    path parameters."""
    for route in scope["app"].router.routes:
        found, child = route.matches(scope)
        if found == Match.FULL:
            sender_route = _BY_ROUTE.get((scope["method"], getattr(route, "path", None)))
            return (sender_route, child.get("path_params", {})) if sender_route else None
    return None


class SignedSenderMiddleware:
    """Pure ASGI, so the body it reads is replayed unchanged to the route."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        found = match(scope) if scope.get("app") is not None else None
        state = scope["app"].state
        config = getattr(state, "config", None)
        db = getattr(state, "db", None)
        headers = Headers(scope=scope)
        if found is None or config is None or db is None or not _authorized(config, headers):
            return await self.app(scope, receive, send)

        body, complete = await _read(receive)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": not complete}
            return await receive()

        if not complete:
            return await Refusal(
                413, "malformed_request", f"body exceeds {MAX_BODY_BYTES} bytes"
            ).response()(scope, replay, send)
        route, path_params = found
        try:
            signed = await check(db, scope, headers, body, route, path_params)
        except Refusal as refusal:
            return await refusal.response()(scope, replay, send)
        if signed is not None:
            scope.setdefault("state", {})["signed_sender"] = signed
        return await self.app(scope, replay, send)


def _authorized(config, headers: Headers) -> bool:
    if not config.api_key:
        return config.security.allow_unauthenticated
    auth = headers.get("authorization", "")
    return api_key_valid(auth[7:] if auth.startswith("Bearer ") else None, config.api_key)


async def _read(receive) -> tuple[bytes, bool]:
    data = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return bytes(data), True
        data.extend(message.get("body", b""))
        if len(data) > MAX_BODY_BYTES:
            return bytes(data), False
        if not message.get("more_body", False):
            return bytes(data), True


def _sender(
    route: SenderRoute, path_params, headers: Headers, body: bytes, any_watched: bool
) -> tuple[str | None, object]:
    """The name a request is made as (or None: the route validates the rest),
    and the parsed body.

    Once any name is watched, the body is checked strictly on every sender
    route, wherever the sender comes from: a duplicate key or a body this
    check can't read is refused before a signature or nonce is looked at."""
    parsed = None
    if body:
        try:
            parsed = signing.strict_json(body)
        except ValueError as exc:  # a duplicate key, or a body the check can't read
            if any_watched:
                # A fixed message: the input is never echoed into the response.
                duplicate = isinstance(exc, signing.DuplicateKey)
                message = "the body repeats a key" if duplicate else "the body isn't readable JSON"
                raise Refusal(422, "malformed_request", message) from exc
    if route.where == "path":
        value = path_params.get(route.field)
    elif route.where == "header":
        value = headers.get("x-backbone-sender")
    else:
        value = parsed.get(route.field) if isinstance(parsed, dict) else None
    return (value if isinstance(value, str) else None), parsed


async def check(db, scope, headers: Headers, body: bytes, route: SenderRoute, path_params):
    """The verified sender, None for an ordinary request, or Refusal."""
    t = now()
    watched = await db.signing.watched(t)
    sender, parsed = _sender(route, path_params, headers, body, bool(watched))
    if sender is None:
        if route.where == "header":
            raise Refusal(403, "signature_required", "a signed request is required")
        return None
    key = signing.same_name(sender)
    entry = watched.get(key)
    method = scope["method"]
    raw_path = scope.get("raw_path")
    path = raw_path.decode("latin-1") if raw_path else scope["path"]
    audit = {"sender_key": key, "sender": sender, "method": method, "path": scope["path"]}

    pending = entry["pending"] if entry else None
    if pending is not None and pending["new_public_key"] is not None:
        # Checked against the key waiting for approval, whatever is admitted.
        outcome = "unsigned"
        if all(h in headers for h in HEADERS):
            try:
                await _verify(
                    db,
                    scope,
                    headers,
                    body,
                    route,
                    path,
                    key,
                    t,
                    public_key=pending["new_public_key"],
                    epoch=pending["new_epoch"],
                    use_nonce=False,
                )
                outcome = "verified"
            except Refusal as refusal:
                outcome = refusal.reason
        await db.signing.audit(kind="observation", outcome=outcome, **audit)

    if entry is None or entry["enrollment"] is None:
        if route.where == "header":
            raise Refusal(403, "not_enrolled", f"'{sender}' has no enrolled key")
        return None

    enrollment = entry["enrollment"]
    try:
        if not any(h in headers for h in HEADERS):
            raise Refusal(403, "signature_required", f"requests made as '{sender}' must be signed")
        shape = SIGNED_SHAPES.get(route.template)
        if shape is not None and isinstance(parsed, dict) and set(parsed) - shape:
            raise Refusal(422, "malformed_request", "the body has a field this route doesn't take")
        confirmed = (
            route.template in CONFIRMABLE
            and isinstance(parsed, dict)
            and parsed.get("owner_confirmation") is not None
        )
        return await _verify(
            db,
            scope,
            headers,
            body,
            route,
            path,
            key,
            t,
            public_key=enrollment["public_key"],
            epoch=enrollment["epoch"],
            use_nonce=not confirmed,
            nonce_in_route=confirmed,
        )
    except Refusal as refusal:
        await db.signing.audit(
            kind="refusal", outcome=refusal.reason, target=_target(body), **audit
        )
        raise


async def _verify(
    db,
    scope,
    headers,
    body,
    route,
    path,
    key,
    t,
    *,
    public_key,
    epoch,
    use_nonce,
    nonce_in_route=False,
) -> SignedSender:
    missing = [h for h in HEADERS if h not in headers]
    if missing:
        raise Refusal(403, "signature_required", f"missing header {missing[0]}")
    try:
        header_epoch = int(headers["x-backbone-key-epoch"])
        timestamp = int(headers["x-backbone-timestamp"])
        signature = signing.b64url_decode(headers["x-backbone-signature"])
    except ValueError as exc:
        raise Refusal(422, "malformed_request", "malformed signature header") from exc
    nonce = headers["x-backbone-nonce"]
    if not signing.NONCE_RE.fullmatch(nonce):
        raise Refusal(422, "malformed_request", "the nonce is 32 lowercase hex characters")
    sender = headers["x-backbone-sender"]
    if signing.same_name(sender) != key:
        raise Refusal(403, "sender_mismatch", "the sender header names another sender")
    audience = headers["x-backbone-audience"]
    if audience != await db.signing.audience():
        raise Refusal(403, "audience_mismatch", "signed for a different Backbone")
    if header_epoch != epoch:
        raise Refusal(403, "key_epoch_unknown", f"no key with epoch {header_epoch} admits requests")
    if abs(t - timestamp) > signing.SKEW_SECONDS:
        raise Refusal(
            403, "timestamp_out_of_window", f"more than {signing.SKEW_SECONDS} seconds off"
        )
    query = scope.get("query_string", b"").decode("utf-8", "replace")
    framed = signing.request_bytes(
        purpose=route.purpose,
        method=scope["method"],
        path=path,
        query=signing.canonical_query(parse_qsl(query, keep_blank_values=True)),
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        audience=audience,
        sender=sender,
        epoch=header_epoch,
    )
    try:
        valid = signing.verify(signing.public_key(public_key), signature, framed)
    except signing.VerifierUnavailable as exc:
        log.error("Signed sender refused: %s", exc)
        raise Refusal(503, "verifier_unavailable", "signatures can't be checked") from exc
    if not valid:
        raise Refusal(403, "signature_invalid", "the signature does not verify")
    request_hash = hashlib.sha256(framed).hexdigest()
    if use_nonce:
        seen, _ = await db.signing.use_nonce(
            sender_key=key, epoch=header_epoch, nonce=nonce, request_hash=request_hash, now=t
        )
        if seen != "new":
            raise Refusal(409, "nonce_reused", "this nonce was already used")
    return SignedSender(sender, key, header_epoch, nonce, timestamp, request_hash, nonce_in_route)


def _target(body: bytes) -> str:
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        return ""
    target = parsed.get("target_session") if isinstance(parsed, dict) else None
    return target if isinstance(target, str) else ""
