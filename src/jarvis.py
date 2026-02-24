"""Jarvis HTTP injection — deliver notifications to Lovely Console."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


async def discover_session(inject_url: str, *, sessions_url: str = "") -> str | None:
    """Discover an active Jarvis session ID from the dashboard.

    Uses explicit sessions_url if provided, otherwise derives from
    inject_url by extracting the base URL up to /api/.
    Returns the first session's ID, or None if unavailable.
    """
    if not sessions_url:
        sessions_url = inject_url.rsplit("/api/", 1)[0] + "/api/sessions"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(sessions_url, timeout=10)
            if resp.status_code != 200:
                log.warning("Session discovery failed: %d", resp.status_code)
                return None
            data = resp.json()
            sessions = data.get("sessions", []) if isinstance(data, dict) else data
            if not sessions:
                log.info("No active Jarvis sessions found")
                return None
            session_id = sessions[0].get("id")
            if session_id:
                log.info("Discovered Jarvis session: %s", session_id)
            return session_id
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        log.warning("Session discovery error: %s", e)
        return None


async def inject_message(
    url: str, message: str, *, session_id: str | None = None, sessions_url: str = ""
) -> bool:
    """POST a notification to the Jarvis injection endpoint.

    When session_id is None, discovers the active session first.
    Returns True on 2xx, False otherwise.
    """
    if session_id is None:
        session_id = await discover_session(url, sessions_url=sessions_url)
        if session_id is None:
            log.warning("No Jarvis session available — delivery deferred")
            return False

    payload = {
        "from": "backbone",
        "via": "backbone",
        "message": message,
        "sessionId": session_id,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            if 200 <= resp.status_code < 300:
                log.info("Jarvis injection delivered: %d", resp.status_code)
                return True
            log.warning(
                "Jarvis injection failed: %s %s", resp.status_code, resp.text[:200]
            )
            return False
    except httpx.HTTPError as e:
        log.warning("Jarvis injection error: %s", e)
        return False
