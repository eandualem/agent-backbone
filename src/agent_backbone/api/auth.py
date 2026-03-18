"""API key authentication dependency."""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)


async def require_api_key(request: Request) -> None:
    """FastAPI dependency that validates Bearer token against BACKBONE_API_KEY.

    Dev mode: if BACKBONE_API_KEY is not set, all requests are allowed.
    """
    api_key = os.environ.get("BACKBONE_API_KEY", "")
    if not api_key:
        log.warning("BACKBONE_API_KEY not set — authentication disabled (dev mode)")
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
