"""API key authentication dependency."""

from __future__ import annotations

import os

from fastapi import HTTPException, Request


async def require_api_key(request: Request) -> None:
    """FastAPI dependency that validates Bearer token against BACKBONE_API_KEY.

    Dev mode: if BACKBONE_API_KEY is not set, all requests are allowed.
    """
    api_key = os.environ.get("BACKBONE_API_KEY", "")
    if not api_key:
        return  # Dev mode — no key = unrestricted
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
