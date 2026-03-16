"""Backbone process orchestration — start/stop Prefect, Gateway, Worker, Telegram."""

from __future__ import annotations

import asyncio
import logging
import os
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

PREFECT_PORT = 4200
PREFECT_API_URL = f"http://127.0.0.1:{PREFECT_PORT}/api"
PREFECT_HEALTH_PROBE_RETRY_INTERVAL_SECONDS = 0.4
PREFECT_STARTUP_PROBE_RETRY_INTERVAL_SECONDS = 0.1
PREFECT_SUPERVISOR_RESTART_DELAY_SECONDS = 2.0


def _prefect_env() -> dict[str, str]:
    """Environment for Prefect subprocesses."""
    return {**os.environ, "PREFECT_API_URL": PREFECT_API_URL}


async def _run_prefect_process(*args: str) -> int:
    """Run a Prefect subprocess attached to the current tmux pane."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=BACKBONE_DIR,
        env=_prefect_env(),
    )
    return await proc.wait()


async def _wait_for_prefect_api() -> None:
    """Wait until the local Prefect API answers health probes."""
    health_url = f"http://127.0.0.1:{PREFECT_PORT}/api/health"
    while not await wait_for_health(health_url, retries=1, interval=0):
        log.warning(
            "Prefect API unavailable at %s; retrying worker bootstrap in %.1fs",
            health_url,
            PREFECT_SUPERVISOR_RESTART_DELAY_SECONDS,
        )
        await asyncio.sleep(PREFECT_SUPERVISOR_RESTART_DELAY_SECONDS)


async def run_prefect_server_supervisor() -> None:
    """Keep the Prefect server running inside a tmux session."""
    while True:
        log.info("Starting Prefect server")
        exit_code = await _run_prefect_process("uv", "run", "prefect", "server", "start")
        log.warning(
            "Prefect server exited with code %s; restarting in %.1fs",
            exit_code,
            PREFECT_SUPERVISOR_RESTART_DELAY_SECONDS,
        )
        await asyncio.sleep(PREFECT_SUPERVISOR_RESTART_DELAY_SECONDS)


async def run_prefect_worker_supervisor(config: BackboneConfig) -> None:
    """Keep the Prefect worker running and re-bootstrap deployments after outages."""
    work_pool_name = config.scheduling.work_pool_name

    while True:
        await _wait_for_prefect_api()

        pool_exit = await _run_prefect_process(
            "uv",
            "run",
            "prefect",
            "work-pool",
            "create",
            work_pool_name,
            "--type",
            "process",
        )
        if pool_exit != 0:
            log.warning(
                "Prefect work-pool create exited with code %s for pool '%s'",
                pool_exit,
                work_pool_name,
            )

        deploy_exit = await _run_prefect_process("uv", "run", "prefect", "deploy", "--all")
        if deploy_exit != 0:
            log.warning("Prefect deploy --all exited with code %s", deploy_exit)

        log.info("Starting Prefect worker (pool: %s)", work_pool_name)
        worker_exit = await _run_prefect_process(
            "uv",
            "run",
            "prefect",
            "worker",
            "start",
            "--pool",
            work_pool_name,
        )
        log.warning(
            "Prefect worker exited with code %s; restarting in %.1fs",
            worker_exit,
            PREFECT_SUPERVISOR_RESTART_DELAY_SECONDS,
        )
        await asyncio.sleep(PREFECT_SUPERVISOR_RESTART_DELAY_SECONDS)


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
                if resp.is_success:
                    return True
                log.debug("Health probe %s returned %d", url, resp.status_code)
            except httpx.HTTPError:
                pass
            if i < retries - 1:
                import asyncio

                await asyncio.sleep(interval)
    return False


async def start_prefect(config: BackboneConfig) -> bool:
    """Start Prefect server in a tmux session."""
    prefect_pid = await pid_for_port(PREFECT_PORT)
    if prefect_pid:
        log.info("Prefect server already running (port %d, pid %d)", PREFECT_PORT, prefect_pid)
        return True

    # Session exists but port not bound = stale session, clean it up
    if await session_exists("prefect"):
        log.warning(
            "Prefect session exists but port %d not bound — cleaning up stale session",
            PREFECT_PORT,
        )
        await stop_session("prefect")

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
        command=[
            "uv",
            "run",
            "python",
            "-m",
            "agent_backbone.services.infrastructure",
            "run-prefect-server",
        ],
        environment={"PREFECT_API_URL": PREFECT_API_URL},
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
        environment={"PREFECT_API_URL": PREFECT_API_URL},
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
    worker_pid = read_pid("worker")
    if worker_pid and await session_exists("backbone-worker"):
        log.info("Worker already running (pid %d)", worker_pid)
        return True

    # Session exists but process dead = stale session, clean it up
    if await session_exists("backbone-worker"):
        log.warning("Worker session exists but process not alive — cleaning up stale session")
        await stop_session("backbone-worker")

    # Clean stale PID
    await stop_by_pid("worker")

    ok = await start_session(
        "backbone-worker",
        working_dir=BACKBONE_DIR,
        command=[
            "uv",
            "run",
            "python",
            "-m",
            "agent_backbone.services.infrastructure",
            "run-prefect-worker",
        ],
        environment={"PREFECT_API_URL": PREFECT_API_URL},
    )
    if ok:
        await record_tmux_pid("worker", "backbone-worker")
        log.info("Worker supervisor started (pool: %s)", config.scheduling.work_pool_name)
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
