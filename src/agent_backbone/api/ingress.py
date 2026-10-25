"""Bounds checked before either HTTP API or Socket.IO parses a request."""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from agent_backbone.api.signed import MAX_BODY_BYTES, _read
from agent_backbone.models import DOCUMENT_WIRE_BYTES


class IngressMiddleware:
    def __init__(self, app, fastapi_app) -> None:
        self.app = app
        self.other_asgi_app = fastapi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        config = self.other_asgi_app.state.config
        if not config.api_key and config.security.allow_unauthenticated:
            try:
                host = urlsplit("//" + headers.get("host", "")).hostname
                allowed_hosts = {"localhost", "127.0.0.1", "::1"}
                if config.backbone.host not in {"0.0.0.0", "::"}:
                    allowed_hosts.add(config.backbone.host)
                origin = headers.get("origin")
                scheme = "https" if scope.get("scheme") in {"https", "wss"} else "http"
                own_origin = f"{scheme}://{headers.get('host', '')}"
                allowed = host in allowed_hosts and (
                    origin is None or origin == own_origin or origin in config.backbone.cors_origins
                )
            except ValueError:
                allowed = False
            if not allowed:
                if scope["type"] == "websocket":
                    return await send({"type": "websocket.close", "code": 1008})
                return await JSONResponse(
                    {"detail": "Host or Origin is not allowed in unauthenticated mode"},
                    status_code=403,
                )(scope, receive, send)
        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        prefix = scope.get("root_path", "").rstrip("/")
        if prefix and path.startswith(prefix + "/"):
            path = path[len(prefix) :]
        limit = DOCUMENT_WIRE_BYTES if path.rstrip("/") == "/api/documents" else MAX_BODY_BYTES
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = -1
        if declared < 0 or declared > limit:
            return await self._too_large(scope, receive, send)
        body, complete = await _read(receive, limit)
        if complete is None:
            return
        if not complete:
            return await self._too_large(scope, receive, send)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)

    async def _too_large(self, scope, receive, send):
        response = JSONResponse(
            {"detail": "Request body exceeds the allowed size"}, status_code=413
        )
        origins = list(self.other_asgi_app.state.config.backbone.cors_origins)
        if origins:
            response = CORSMiddleware(
                response,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        return await response(scope, receive, send)
