"""Authenticate and compare a captured provider identity before content access."""

from contextlib import AsyncExitStack

from fastapi import HTTPException, Request
from sqlalchemy.engine import make_url
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from agent_backbone.api.auth import require_api_key
from agent_backbone.config import BackboneConfig, agent_credential
from agent_backbone.services.database import BackboneDB, ProfileProvider
from agent_backbone.services.terminal import tmux_server_socket

PROFILE_HEADERS = (
    "x-backbone-profile-id",
    "x-backbone-installation-id",
    "x-backbone-lifecycle-id",
)
PROFILE_FIELDS = ("profile_id", "installation_id", "lifecycle_id")
UNBOUND_ROUTES = frozenset({("GET", "/health"), ("POST", "/webhooks/github")})
BINDING_MISMATCH = {"code": "binding_mismatch", "message": "Profile binding does not match."}
BINDING_REQUIRED = {"code": "binding_required", "message": "Profile binding is required."}


async def provider_current(provider: ProfileProvider, app) -> bool:
    try:
        return await provider_context_current(provider, app.state.config, app.state.db)
    except Exception:
        return False


async def provider_context_current(
    provider: ProfileProvider, config: BackboneConfig, db: BackboneDB
) -> bool:
    """Never expose metadata or storage failures in a binding refusal."""
    if tmux_server_socket() != str(provider.socket_path):
        return False
    try:
        url = make_url(db.url)
        if (
            config.data_dir != provider.reservation.data_dir
            or url.get_backend_name() != "sqlite"
            or url.database != str(provider.database_path)
            or url.query
            or await db.profile_identity.read() != provider.reservation.identity
        ):
            return False
        await provider.check()
    except Exception:
        return False
    return True


async def binding_error(provider: ProfileProvider | None, values: dict, app) -> dict | None:
    if provider is None:
        return BINDING_MISMATCH if values else None
    if not values:
        # Unbound local clients belong only to the Default profile.
        if provider.reservation.kind != "legacy_default":
            return BINDING_REQUIRED
        return None if await provider_current(provider, app) else BINDING_MISMATCH
    identity = provider.reservation.identity
    if values != {field: getattr(identity, field) for field in PROFILE_FIELDS}:
        return BINDING_MISMATCH
    if not await provider_current(provider, app):
        return BINDING_MISMATCH
    return None


async def profile_identity(request: Request) -> dict:
    """Authenticated handshake; unadopted providers have no attestable identity."""
    await require_api_key(request)
    provider = getattr(request.state, "profile_provider", None)
    if provider is None:
        raise HTTPException(status_code=404, detail="Not Found")
    # Middleware has already authenticated and checked the complete binding.
    return provider.identity_response()


class ProfileBindingMiddleware:
    """An unadopted provider cannot attest any requested profile identity.

    Run outside signed-sender processing so a rejected request cannot consume a
    nonce or reach a content handler. Presence, including an empty/partial tuple,
    is binding intent; it must never be interpreted as an unbound legacy request.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        provider = getattr(scope["app"].state, "profile_provider", None)
        headers = Headers(scope=scope)
        values = {
            field: headers[header]
            for header, field in zip(PROFILE_HEADERS, PROFILE_FIELDS, strict=True)
            if header in headers
        }
        if provider is None and not values:
            return await self.app(scope, receive, send)
        path = scope["path"]
        prefix = scope.get("root_path", "").rstrip("/")
        if prefix and path.startswith(prefix + "/"):
            path = path[len(prefix) :]
        exempt = not values and (scope["method"], path) in UNBOUND_ROUTES
        # An agent credential derives from this provider's key and authenticates
        # nowhere else, so an agent's own CLI needs no identity headers.
        agent = not values and agent_credential(
            headers.get("authorization", "").removeprefix("Bearer ")
        )
        if not exempt:
            try:
                await require_api_key(Request(scope))
            except HTTPException as exc:
                return await JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )(scope, receive, send)
        async with AsyncExitStack() as operations:
            try:
                if provider is not None:
                    await operations.enter_async_context(provider.operation())
            except (ValueError, OSError):
                return await JSONResponse({"detail": BINDING_MISMATCH}, status_code=409)(
                    scope, receive, send
                )
            if exempt or agent:
                # Preserve the route's authentication. Hold its fence through the
                # entire response, just as for authenticated bound content.
                error = None if await provider_current(provider, scope["app"]) else BINDING_MISMATCH
            else:
                error = await binding_error(provider, values, scope["app"])
                if any(len(headers.getlist(header)) > 1 for header in PROFILE_HEADERS):
                    error = BINDING_MISMATCH
            if error is None:
                scope.setdefault("state", {})["profile_provider"] = provider
                return await self.app(scope, receive, send)
            return await JSONResponse({"detail": error}, status_code=409)(scope, receive, send)
