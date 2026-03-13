"""Shared command builders for infrastructure-managed sessions."""

from __future__ import annotations

import os

from agent_backbone.services.terminal import RUNTIME_ENV_KEY


def build_agent_command(cli: str, model: str | None = None) -> list[str]:
    """Build the CLI command used to launch an agent session."""
    command = [cli]
    if model:
        command.extend(["--model", model])
    return command


def build_prefect_server_command() -> list[str]:
    """Build the Prefect server startup command."""
    return ["uv", "run", "prefect", "server", "start"]


def build_gateway_command(port: int) -> list[str]:
    """Build the uvicorn command for the gateway."""
    return [
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
    ]


def build_prefect_pool_create_command(work_pool_name: str) -> list[str]:
    """Build the idempotent Prefect work-pool creation command."""
    return [
        "uv",
        "run",
        "prefect",
        "work-pool",
        "create",
        work_pool_name,
        "--type",
        "process",
    ]


def build_prefect_deploy_command() -> list[str]:
    """Build the deployment refresh command for all scheduled flows."""
    return ["uv", "run", "prefect", "deploy", "--all"]


def build_worker_command(work_pool_name: str) -> list[str]:
    """Build the Prefect worker startup command."""
    return ["uv", "run", "prefect", "worker", "start", "--pool", work_pool_name]


def build_telegram_command() -> list[str]:
    """Build the Telegram bot session command."""
    return [
        "uv",
        "run",
        "python",
        "-m",
        "agent_backbone.services.infrastructure",
        "run-telegram-bot",
    ]


def build_ngrok_command(port: int) -> list[str]:
    """Build the ngrok tunnel command for the gateway port."""
    return ["ngrok", "http", str(port)]


def prefect_environment(api_url: str) -> dict[str, str]:
    """Build the standard Prefect environment payload."""
    return {"PREFECT_API_URL": api_url}


def runtime_environment(cli: str) -> dict[str, str]:
    """Build the standard runtime environment payload for agent sessions."""
    return {RUNTIME_ENV_KEY: cli}


def inherited_environment(extra: dict[str, str]) -> dict[str, str]:
    """Merge the current process environment with extra variables."""
    return {**os.environ, **extra}
