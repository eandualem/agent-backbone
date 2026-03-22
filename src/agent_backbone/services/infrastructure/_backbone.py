"""Backbone process orchestration — start/stop Gateway, Telegram."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from agent_backbone.services.infrastructure._processes import (
    check_port_free,
    kill_port_process,
    pid_for_port,
    read_pid,
    record_tmux_pid,
    remove_pid,
    stop_by_pid,
)
from agent_backbone.services.terminal import session_exists, start_session, stop_session

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

# Repo root — 4 parents up from this file:
# _backbone.py -> infrastructure/ -> services/ -> agent_backbone/ -> src/ -> repo root
BACKBONE_DIR = str(Path(__file__).resolve().parents[4])


async def wait_for_health(
    url: str,
    retries: int = 10,
    interval: float = 0.4,
) -> bool:
    """Wait for an HTTP endpoint to return a successful response."""
    import asyncio

    async with httpx.AsyncClient(timeout=5.0) as client:
        for i in range(retries):
            try:
                resp = await client.get(url)
                if resp.is_success:
                    return True
                log.debug("Health probe %s returned %d", url, resp.status_code)
            except httpx.HTTPError:
                pass
            if i < retries - 1:
                await asyncio.sleep(interval)
    return False


async def start_gateway(config: BackboneConfig) -> bool:
    """Start gateway server in a tmux session."""
    port = config.gateway.port
    gateway_pid = await pid_for_port(port)
    if gateway_pid:
        log.info("Gateway already running (port %d, pid %d)", port, gateway_pid)
        return True

    # Session exists but port not bound = stale session, clean it up
    if await session_exists("gateway"):
        log.warning(
            "Gateway session exists but port %d not bound — cleaning up stale session", port
        )
        await stop_session("gateway")

    # Clean stale state
    await stop_by_pid("gateway")
    await kill_port_process(port)

    ok = await start_session(
        "gateway",
        working_dir=BACKBONE_DIR,
        command=[
            "uv",
            "run",
            "uvicorn",
            "agent_backbone.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--reload",
            "--reload-dir",
            "src",
            "--reload-include",
            "*.toml",
            "--log-level",
            "info",
        ],
    )
    if ok:
        await record_tmux_pid("gateway", "gateway")
        log.info("Gateway started (port %d, auto-reload enabled)", port)
    return ok


async def stop_gateway(config: BackboneConfig) -> bool:
    """Stop gateway server."""
    port = config.gateway.port
    await stop_session("gateway")
    await stop_by_pid("gateway")
    await kill_port_process(port)
    remove_pid("gateway")
    log.info("Gateway stopped")
    return True


async def restart_gateway(config: BackboneConfig) -> bool:
    """Restart gateway server."""
    await stop_gateway(config)
    return await start_gateway(config)


async def start_telegram(config: BackboneConfig) -> bool:
    """Start Telegram bot in a tmux session."""
    telegram_pid = read_pid("telegram")
    if telegram_pid and await session_exists("telegram-bot"):
        log.info("Telegram bot already running (pid %d)", telegram_pid)
        return True

    # Session exists but process dead = stale session, clean it up
    if await session_exists("telegram-bot"):
        log.warning("Telegram session exists but process not alive — cleaning up stale session")
        await stop_session("telegram-bot")

    # Clean stale PID
    await stop_by_pid("telegram")

    ok = await start_session(
        "telegram-bot",
        working_dir=BACKBONE_DIR,
        command=[
            "uv",
            "run",
            "python",
            "-m",
            "agent_backbone.services.infrastructure",
            "run-telegram-bot",
        ],
    )
    if ok:
        await record_tmux_pid("telegram", "telegram-bot")
        log.info("Telegram bot started")
    return ok


async def stop_telegram(config: BackboneConfig) -> bool:
    """Stop Telegram bot."""
    await stop_session("telegram-bot")
    await stop_by_pid("telegram")
    remove_pid("telegram")
    log.info("Telegram bot stopped")
    return True


async def start_backbone(config: BackboneConfig) -> bool:
    """Start all backbone services: Gateway + Telegram."""
    gw_ok = await start_gateway(config)
    tg_ok = await start_telegram(config)
    log.info("Backbone started (Gateway + Telegram)")
    return gw_ok and tg_ok


async def stop_backbone(config: BackboneConfig) -> bool:
    """Stop all backbone services in reverse order."""
    await stop_telegram(config)
    await stop_gateway(config)
    log.info("Backbone stopped")
    return True


async def restart_backbone(config: BackboneConfig) -> bool:
    """Restart all backbone services with port verification."""
    await stop_backbone(config)

    port = config.gateway.port
    if not await check_port_free(port):
        log.error("Port %d still occupied after stop", port)
        return False

    return await start_backbone(config)
