"""Cursor telemetry adapter."""

from __future__ import annotations

from pathlib import Path

from agent_backbone.services.telemetry._common import (
    cursor_project_slug,
    extract_text_parts,
    mtime,
    read_jsonl_since,
    read_text_since,
)
from agent_backbone.services.telemetry.interface import TelemetryAdapter
from agent_backbone.services.telemetry.models import (
    TelemetryBatch,
    TelemetryEvent,
    TelemetrySource,
    TelemetrySourceKind,
)
from agent_backbone.services.terminal import TerminalRuntime


class CursorTelemetryAdapter(TelemetryAdapter):
    """Degraded adapter for Cursor project transcripts and log surfaces."""

    runtime = TerminalRuntime.CURSOR

    def __init__(self, projects_dir: Path | None = None) -> None:
        self._projects_dir = projects_dir or (Path.home() / ".cursor" / "projects")

    def discover_sources(
        self,
        *,
        session_name: str,
        cwd: Path,
        entity: str | None = None,
    ) -> list[TelemetrySource]:
        project_dir = self._projects_dir / cursor_project_slug(cwd)
        if not project_dir.exists():
            return []

        sources: list[TelemetrySource] = []
        for path in sorted((project_dir / "agent-transcripts").rglob("*.jsonl"), key=mtime):
            sources.append(
                TelemetrySource(
                    session=session_name,
                    entity=entity,
                    runtime=self.runtime,
                    source_kind=TelemetrySourceKind.JSONL,
                    path=path,
                )
            )
        for path in sorted((project_dir / "agent-transcripts").glob("*.txt"), key=mtime):
            sources.append(
                TelemetrySource(
                    session=session_name,
                    entity=entity,
                    runtime=self.runtime,
                    source_kind=TelemetrySourceKind.LOG,
                    path=path,
                    metadata={"surface": "transcript"},
                )
            )
        for path in sorted((project_dir / "agent-tools").glob("*.txt"), key=mtime):
            sources.append(
                TelemetrySource(
                    session=session_name,
                    entity=entity,
                    runtime=self.runtime,
                    source_kind=TelemetrySourceKind.LOG,
                    path=path,
                    metadata={"surface": "tool"},
                )
            )
        for path in sorted((project_dir / "terminals").glob("*.txt"), key=mtime):
            sources.append(
                TelemetrySource(
                    session=session_name,
                    entity=entity,
                    runtime=self.runtime,
                    source_kind=TelemetrySourceKind.LOG,
                    path=path,
                    metadata={"surface": "terminal"},
                )
            )
        return sources

    def read_since(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        if source.source_kind == TelemetrySourceKind.JSONL:
            return self._read_jsonl(source, checkpoint)
        return self._read_text_log(source, checkpoint)

    def _read_jsonl(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        records, next_checkpoint = read_jsonl_since(source.path, checkpoint)
        events: list[TelemetryEvent] = []
        last_event_ts: float | None = None
        for line_offset, record in records:
            role = str(record.get("role") or "")
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            ts = source.path.stat().st_mtime
            event_name = "message.user" if role == "user" else "message.assistant"
            events.append(
                TelemetryEvent(
                    session=source.session,
                    entity=source.entity,
                    event=event_name,
                    ts=ts,
                    runtime=self.runtime,
                    source_kind=source.source_kind,
                    source_ref=source.source_ref,
                    source_event_id=f"{source.path.name}:{line_offset}",
                    payload={
                        "role": role,
                        "content": extract_text_parts(content),
                    },
                )
            )
            last_event_ts = ts
        return TelemetryBatch(
            source=source,
            events=events,
            checkpoint=next_checkpoint,
            last_event_ts=last_event_ts,
        )

    def _read_text_log(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        delta, next_checkpoint = read_text_since(source.path, checkpoint)
        if not delta:
            return TelemetryBatch(source=source, events=[], checkpoint=next_checkpoint)

        surface = source.metadata.get("surface", "log")
        event_name = {
            "tool": "tool.finished",
            "terminal": "runtime.event",
            "transcript": "runtime.event",
        }.get(surface, "runtime.event")
        ts = source.path.stat().st_mtime
        return TelemetryBatch(
            source=source,
            events=[
                TelemetryEvent(
                    session=source.session,
                    entity=source.entity,
                    event=event_name,
                    ts=ts,
                    runtime=self.runtime,
                    source_kind=source.source_kind,
                    source_ref=source.source_ref,
                    source_event_id=f"{source.path.name}:{next_checkpoint['offset']}",
                    payload={
                        "surface": surface,
                        "path": source.path.name,
                        "content": delta,
                    },
                )
            ],
            checkpoint=next_checkpoint,
            last_event_ts=ts,
        )
