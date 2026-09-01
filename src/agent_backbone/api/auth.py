"""API key authentication dependency."""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

_warned_unauthenticated = False


def _configured_api_key(request: Request) -> tuple[str, bool]:
    """Return (api_key, allow_unauthenticated) from the app config."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        return "", False
    return config.api_key, config.security.allow_unauthenticated


def api_key_valid(candidate: str | None, api_key: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, api_key)


async def require_api_key(request: Request) -> None:
    """FastAPI dependency that validates a Bearer token against the configured API key.

    Without an API key the API refuses requests unless
    ``[security] allow_unauthenticated = true`` (or ``BACKBONE_ALLOW_UNAUTHENTICATED=1``).
    """
    global _warned_unauthenticated
    api_key, allow_unauthenticated = _configured_api_key(request)
    if not api_key:
        if allow_unauthenticated:
            if not _warned_unauthenticated:
                log.warning("API authentication DISABLED (allow_unauthenticated is set)")
                _warned_unauthenticated = True
            return
        raise HTTPException(
            status_code=401,
            detail="No API key configured. Set BACKBONE_API_KEY (run `backbone init`).",
        )
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    if not api_key_valid(token, api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
