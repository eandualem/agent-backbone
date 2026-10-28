"""API key authentication dependency."""

from __future__ import annotations

import hmac
import logging
import re

from fastapi import HTTPException, Request

from agent_backbone.config import REGISTRATION_KEY_PREFIX, agent_api_key

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


def credential_registration(candidate: str | None, operator_key: str) -> str | None:
    """Return a cryptographically bound registration, never a caller-supplied label."""
    if not operator_key or not candidate:
        return None
    match = re.fullmatch(
        re.escape(REGISTRATION_KEY_PREFIX) + r"([a-f0-9]{32}):[a-f0-9]{64}", candidate
    )
    if match and api_key_valid(candidate, agent_api_key(operator_key, match[1])):
        return match[1]
    return None


def credential_role(candidate: str | None, operator_key: str) -> str | None:
    if api_key_valid(candidate, operator_key):
        return "operator"
    if operator_key and (
        api_key_valid(candidate, agent_api_key(operator_key))
        or credential_registration(candidate, operator_key)
    ):
        return "agent"
    return None


async def require_operator(request: Request) -> None:
    await require_api_key(request)
    if request.state.credential_role != "operator":
        raise HTTPException(
            status_code=403, detail="This operation requires an operator credential"
        )


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
            request.state.credential_role = "operator"
            return
        raise HTTPException(
            status_code=401,
            detail="No API key configured. Set BACKBONE_API_KEY (run `backbone init`).",
        )
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    role = credential_role(token, api_key)
    if role is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    registration = credential_registration(token, api_key) if role == "agent" else None
    name = (
        await request.app.state.db.agents.registration_name(registration) if registration else None
    )
    if registration and not name:
        raise HTTPException(status_code=403, detail="Agent credential registration is retired")
    request.state.credential_registration = registration
    request.state.credential_agent = name
    if role == "agent" and (identity := request.headers.get("X-Backbone-Client-Registration")):
        if registration and identity != registration:
            raise HTTPException(
                status_code=403, detail="Agent credential registration does not match"
            )
        name = request.headers.get("X-Backbone-Client-Agent", "")
        if not await request.app.state.db.agents.registration_matches(name, identity):
            raise HTTPException(status_code=403, detail="Agent credential registration is retired")
    request.state.credential_role = role
