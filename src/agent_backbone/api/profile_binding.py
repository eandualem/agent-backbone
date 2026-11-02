"""Fail closed for profile-bound clients until provider adoption is implemented."""

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from agent_backbone.api.auth import require_api_key

PROFILE_HEADERS = (
    "x-backbone-profile-id",
    "x-backbone-installation-id",
    "x-backbone-lifecycle-id",
)
PROFILE_FIELDS = ("profile_id", "installation_id", "lifecycle_id")
BINDING_MISMATCH = {"code": "binding_mismatch", "message": "Profile binding does not match."}


class LegacyProfileBindingMiddleware:
    """An unadopted provider cannot attest any requested profile identity.

    Run outside signed-sender processing so a rejected request cannot consume a
    nonce or reach a content handler. Presence, including an empty/partial tuple,
    is binding intent; it must never be interpreted as an unbound legacy request.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not any(
            field in Headers(scope=scope) for field in PROFILE_HEADERS
        ):
            return await self.app(scope, receive, send)
        try:
            await require_api_key(Request(scope))
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
        else:
            response = JSONResponse({"detail": BINDING_MISMATCH}, status_code=409)
        return await response(scope, receive, send)
