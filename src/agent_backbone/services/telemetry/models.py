"""Normalized models for CLI-native telemetry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from agent_backbone.services.terminal import TerminalRuntime


class TelemetrySourceKind(StrEnum):
    """Kinds of native telemetry sources."""

    FILE = "file"
    JSONL = "jsonl"
    SQLITE = "sqlite"
    SNAPSHOT = "json_snapshot"
    LOG = "log"


@dataclass(frozen=True, slots=True)
class TelemetrySource:
    """One native telemetry source owned by a runtime adapter."""

    session: str
    runtime: TerminalRuntime
    source_kind: TelemetrySourceKind
    path: Path
    entity: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def source_ref(self) -> str:
        return self.metadata.get("source_ref", str(self.path))


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """Normalized telemetry event stored in the database."""

    session: str
    event: str
    ts: float
    entity: str | None = None
    runtime: TerminalRuntime = TerminalRuntime.UNKNOWN
    source_kind: TelemetrySourceKind = TelemetrySourceKind.FILE
    source_ref: str = ""
    source_event_id: str | None = None
    trace_id: str | None = None
    parent_trace_id: str | None = None
    model: str | None = None
    payload: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class TelemetryBatch:
    """One incremental read result for a telemetry source."""

    source: TelemetrySource
    events: list[TelemetryEvent]
    checkpoint: dict[str, object]
    last_event_ts: float | None = None


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    """Collector result summary for logging/tests."""

    runtime: TerminalRuntime
    sources_scanned: int
    events_recorded: int
    updated_sources: list[str]
