"""Ngrok tunnel management — start, stop, URL retrieval."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from agent_backbone.services.infrastructure._processes import kill_port_process
from agent_backbone.services.terminal import session_exists, start_session, stop_session

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

NGROK_API_PORT = 4040


async def start_tunnel(config: BackboneConfig) -> bool:
    """Start ngrok tunnel in a tmux session."""
    if await session_exists("ngrok"):
        log.info("ngrok tunnel already running")
        url = await get_tunnel_url()
        if url:
            log.info("Public URL: %s", url)
        return True

    port = config.gateway.port
    ok = await start_session(
        "ngrok",
        command=["ngrok", "http", str(port)],
    )
    if ok:
        log.info("ngrok tunnel started (port %d)", port)
        # Brief pause for ngrok to initialize
        import asyncio

        await asyncio.sleep(2)
        url = await get_tunnel_url()
        if url:
            log.info("Public URL: %s/webhook", url)
    return ok


async def stop_tunnel() -> bool:
    """Stop ngrok tunnel."""
    session_ok = await stop_session("ngrok")
    port_ok = await kill_port_process(NGROK_API_PORT)
    log.info("ngrok tunnel stopped")
    return session_ok and port_ok


async def get_tunnel_url() -> str | None:
    """Query ngrok API for the public tunnel URL."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://127.0.0.1:4040/api/tunnels")
            if resp.status_code == 200:
                tunnels = resp.json().get("tunnels", [])
                if tunnels:
                    return tunnels[0].get("public_url")
    except httpx.HTTPError:
        pass
    return None
