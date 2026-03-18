"""Telemetry adapter interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from agent_backbone.services.telemetry.models import TelemetryBatch, TelemetrySource
from agent_backbone.services.terminal import TerminalRuntime


class TelemetryAdapter(ABC):
    """Contract for reading one CLI's native telemetry."""

    runtime: TerminalRuntime = TerminalRuntime.UNKNOWN

    @abstractmethod
    def discover_sources(
        self,
        *,
        session_name: str,
        cwd: Path,
        entity: str | None = None,
    ) -> list[TelemetrySource]:
        """Return native telemetry sources for a tmux session."""

    @abstractmethod
    def read_since(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        """Read and normalize all events newer than the checkpoint."""
