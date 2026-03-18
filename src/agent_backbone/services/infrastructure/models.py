"""Infrastructure service data models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ServiceName(StrEnum):
    """Known backbone infrastructure services."""

    PREFECT = "prefect"
    GATEWAY = "gateway"
    WORKER = "backbone-worker"
    TELEGRAM = "telegram-bot"


@dataclass
class ServiceStatus:
    """Status of a single infrastructure service."""

    name: str
    session: str
    running: bool
    port_open: bool = False
    pid: int | None = None
    extra: str = ""
