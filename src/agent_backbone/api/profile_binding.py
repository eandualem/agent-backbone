"""Fail closed for profile-bound clients until provider adoption is implemented."""

import asyncio
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from agent_backbone.api.auth import require_api_key
from agent_backbone.config import PROFILE_REGISTRY_PATH
from agent_backbone.services.database import BackboneDB, ProfileRegistry

PROFILE_HEADERS = (
    "x-backbone-profile-id",
    "x-backbone-installation-id",
    "x-backbone-lifecycle-id",
)
PROFILE_FIELDS = ("profile_id", "installation_id", "lifecycle_id")
BINDING_MISMATCH = {"code": "binding_mismatch", "message": "Profile binding does not match."}


async def check_profile_startup(db: BackboneDB, data_dir: Path) -> None:
    """Do not start services for a reservation or silently treat it as legacy."""
    identity = await db.profile_identity.read()
    registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
    if identity is None:
        await asyncio.to_thread(registry.check_unadopted, data_dir.resolve())
        return
    await asyncio.to_thread(registry.check_reservation, identity, data_dir.resolve())
    # Matching reservations are still non-ready. Activation must establish the
    # complete provider boundary before any agents, integrations or jobs start.
    raise ValueError("Profile reservation is not ready; activation is not implemented")


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
