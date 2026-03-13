"""Backbone process orchestration — start/stop Prefect, Gateway, Worker, Telegram."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from agent_backbone.services.infrastructure._commands import (
    build_gateway_command,
    build_prefect_deploy_command,
    build_prefect_pool_create_command,
    build_prefect_server_command,
    build_telegram_command,
    build_worker_command,
    inherited_environment,
    prefect_environment,
)
from agent_backbone.services.infrastructure._processes import (
    check_port_free,
    kill_port_process,
    record_tmux_pid,
    remove_pid,
    stop_by_pid,
)
from agent_backbone.services.infrastructure._tunnel import stop_tunnel
from agent_backbone.services.terminal import session_exists, start_session, stop_session

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

# Repo root — 4 parents up from this file:
# _backbone.py -> infrastructure/ -> services/ -> agent_backbone/ -> src/ -> repo root
BACKBONE_DIR = str(Path(__file__).resolve().parents[4])

PREFECT_PORT = 4200
PREFECT_API_URL = f"http://127.0.0.1:{PREFECT_PORT}/api"
PREFECT_HEALTH_PROBE_RETRY_INTERVAL_SECONDS = 0.4
PREFECT_STARTUP_PROBE_RETRY_INTERVAL_SECONDS = 0.1


async def wait_for_health(
    url: str,
    retries: int = 10,
    interval: float = PREFECT_HEALTH_PROBE_RETRY_INTERVAL_SECONDS,
) -> bool:
    """Wait for an HTTP endpoint to return a successful response."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        for i in range(retries):
            try:
                resp = await client.get(url)
                if resp.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            if i < retries - 1:
                await asyncio.sleep(interval)
    return False


async def start_prefect(config: BackboneConfig) -> bool:
    """Start Prefect server in a tmux session."""
    del config
    if await session_exists("prefect"):
        log.info("Prefect server already running")
        return True

    # Clean stale PID
    stop_by_pid_result = await stop_by_pid("prefect")
    if not stop_by_pid_result:
        log.warning("Could not clean stale prefect PID")

    if not await check_port_free(PREFECT_PORT):
        log.error("Port %d already in use", PREFECT_PORT)
        return False

    ok = await start_session(
        "prefect",
        working_dir=BACKBONE_DIR,
        command=build_prefect_server_command(),
    )
    if ok:
        await record_tmux_pid("prefect", "prefect")
        log.info("Prefect server started (port %d)", PREFECT_PORT)
    return ok


async def stop_prefect(config: BackboneConfig) -> bool:
    """Stop Prefect server."""
    await stop_session("prefect")
    await stop_by_pid("prefect")
    await kill_port_process(PREFECT_PORT)
    remove_pid("prefect")
    log.info("Prefect server stopped")
    return True


async def start_gateway(config: BackboneConfig) -> bool:
    """Start gateway server in a tmux session."""
    port = config.gateway.port
    if await session_exists("gateway"):
        log.info("Gateway already running")
        return True

    # Clean stale state
    await stop_by_pid("gateway")
    await kill_port_process(port)

    ok = await start_session(
        "gateway",
        working_dir=BACKBONE_DIR,
        command=build_gateway_command(port),
        environment=prefect_environment(PREFECT_API_URL),
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


async def start_worker(config: BackboneConfig) -> bool:
    """Start Prefect worker in a tmux session."""
    if await session_exists("backbone-worker"):
        log.info("Worker already running")
        return True

    # Clean stale PID
    await stop_by_pid("worker")

    work_pool_name = config.scheduling.work_pool_name

    # Create work pool (idempotent)
    pool_proc = await asyncio.create_subprocess_exec(
        *build_prefect_pool_create_command(work_pool_name),
        cwd=BACKBONE_DIR,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=inherited_environment(prefect_environment(PREFECT_API_URL)),
    )
    await pool_proc.wait()

    # Deploy all scheduled flows
    deploy_proc = await asyncio.create_subprocess_exec(
        *build_prefect_deploy_command(),
        cwd=BACKBONE_DIR,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=inherited_environment(prefect_environment(PREFECT_API_URL)),
    )
    await deploy_proc.wait()

    ok = await start_session(
        "backbone-worker",
        working_dir=BACKBONE_DIR,
        command=build_worker_command(work_pool_name),
        environment=prefect_environment(PREFECT_API_URL),
    )
    if ok:
        await record_tmux_pid("worker", "backbone-worker")
        log.info("Worker started (pool: %s)", work_pool_name)
    return ok


async def stop_worker(config: BackboneConfig) -> bool:
    """Stop Prefect worker."""
    await stop_session("backbone-worker")
    await stop_by_pid("worker")
    remove_pid("worker")
    log.info("Worker stopped")
    return True


async def start_telegram(config: BackboneConfig) -> bool:
    """Start Telegram bot in a tmux session."""
    del config
    if await session_exists("telegram-bot"):
        log.info("Telegram bot already running")
        return True

    # Clean stale PID
    await stop_by_pid("telegram")

    ok = await start_session(
        "telegram-bot",
        working_dir=BACKBONE_DIR,
        command=build_telegram_command(),
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
    """Start all backbone services in order: Prefect -> Gateway -> Worker -> Telegram."""
    ok = await start_prefect(config)
    if not ok:
        return False

    log.info("Waiting for Prefect server health...")
    healthy = await wait_for_health(
        f"http://127.0.0.1:{PREFECT_PORT}/api/health",
        interval=PREFECT_STARTUP_PROBE_RETRY_INTERVAL_SECONDS,
    )
    if not healthy:
        log.warning("Prefect server health check failed, continuing anyway")

    gw_ok = await start_gateway(config)
    wk_ok = await start_worker(config)
    tg_ok = await start_telegram(config)
    log.info("Backbone started (Prefect + Gateway + Worker + Telegram)")
    return gw_ok and wk_ok and tg_ok


async def stop_backbone(config: BackboneConfig) -> bool:
    """Stop all backbone services in reverse order."""
    await stop_telegram(config)
    await stop_gateway(config)
    await stop_worker(config)
    await stop_prefect(config)
    await stop_tunnel()
    log.info("Backbone stopped")
    return True


async def restart_backbone(config: BackboneConfig) -> bool:
    """Restart all backbone services with port verification."""
    await stop_backbone(config)

    port = config.gateway.port
    if not await check_port_free(PREFECT_PORT):
        log.error("Port %d still occupied after stop", PREFECT_PORT)
        return False
    if not await check_port_free(port):
        log.error("Port %d still occupied after stop", port)
        return False

    return await start_backbone(config)
