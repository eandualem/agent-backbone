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
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

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
    path: re.Pattern[str]
    where: str  # "body", "path" or "header"
    field: str
    purpose: str = "request"


def _route(method: str, path: str, where: str, field: str, purpose: str = "request"):
    return SenderRoute(method, re.compile(path), where, field, purpose)


SENDER_ROUTES: tuple[SenderRoute, ...] = (
    _route("POST", r"/api/messages", "body", "from_entity"),
    _route("POST", r"/api/steer", "body", "from_entity"),
    _route("POST", r"/api/messages/inbox", "body", "session"),
    _route("POST", r"/api/agents/[^/]+/(?:approve|deny)", "body", "from_entity"),
    _route("POST", r"/api/agents/[^/]+/restart", "body", "from_entity"),
    _route("POST", r"/api/integrations/reply", "body", "session"),
    _route("POST", r"/api/reports", "body", "agent"),
    _route("POST", r"/api/swarms", "body", "initiator"),
    _route("POST", r"/api/agents/(?P<name>[^/]+)/state", "path", "name"),
    _route("POST", r"/api/skills", "body", "actor"),
    _route("PUT", r"/api/skills/[^/]+/tags", "body", "actor"),
    _route("POST", r"/api/signing/rotation", "header", "", "rotate"),
)


@dataclass(frozen=True)
class SignedSender:
    """A verified request, handed to the route as ``request.state.signed_sender``."""

    sender: str
    sender_key: str
    epoch: int
    nonce: str
    timestamp: int


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


def match(method: str, path: str) -> tuple[SenderRoute, re.Match[str]] | None:
    for route in SENDER_ROUTES:
        if route.method == method:
            found = route.path.fullmatch(path)
            if found is not None:
                return route, found
    return None


class SignedSenderMiddleware:
    """Pure ASGI, so the body it reads is replayed unchanged to the route."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        found = match(scope["method"], scope["path"])
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
        route, path_match = found
        try:
            signed = await check(db, scope, headers, body, route, path_match)
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


def _sender(route: SenderRoute, path_match, headers: Headers, body: bytes, any_watched: bool):
    """The name a request is made as, or None (the route validates the rest)."""
    if route.where == "path":
        return unquote(path_match.group(route.field))
    if route.where == "header":
        return headers.get("x-backbone-sender")
    try:
        parsed = signing.strict_json(body) if body else None
    except signing.DuplicateKey as exc:
        if any_watched:
            raise Refusal(422, "malformed_request", str(exc)) from exc
        return None
    except ValueError:
        return None
    value = parsed.get(route.field) if isinstance(parsed, dict) else None
    return value if isinstance(value, str) else None


async def check(db, scope, headers: Headers, body: bytes, route: SenderRoute, path_match):
    """The verified sender, None for an ordinary request, or Refusal."""
    t = now()
    watched = await db.signing.watched(t)
    sender = _sender(route, path_match, headers, body, bool(watched))
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

    if entry is None or entry["enrollment"] is None:
        pending = entry["pending"] if entry else None
        if pending is not None:
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
                        epoch=pending["expected_epoch"] + 1,
                        use_nonce=False,
                    )
                    outcome = "verified"
                except Refusal as refusal:
                    outcome = refusal.reason
            await db.signing.audit(kind="observation", outcome=outcome, **audit)
        if route.where == "header":
            raise Refusal(403, "not_enrolled", f"'{sender}' has no enrolled key")
        return None

    enrollment = entry["enrollment"]
    try:
        if not any(h in headers for h in HEADERS):
            raise Refusal(403, "signature_required", f"requests made as '{sender}' must be signed")
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
            use_nonce=True,
        )
    except Refusal as refusal:
        await db.signing.audit(
            kind="refusal", outcome=refusal.reason, target=_target(body), **audit
        )
        raise


async def _verify(
    db, scope, headers, body, route, path, key, t, *, public_key, epoch, use_nonce
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
    if use_nonce:
        seen, _ = await db.signing.use_nonce(
            sender_key=key,
            epoch=header_epoch,
            nonce=nonce,
            request_hash=hashlib.sha256(framed).hexdigest(),
            now=t,
        )
        if seen != "new":
            raise Refusal(409, "nonce_reused", "this nonce was already used")
    return SignedSender(sender, key, header_epoch, nonce, timestamp)


def _target(body: bytes) -> str:
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        return ""
    target = parsed.get("target_session") if isinstance(parsed, dict) else None
    return target if isinstance(target, str) else ""
