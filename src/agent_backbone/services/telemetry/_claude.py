"""Claude telemetry adapter."""

from __future__ import annotations

from pathlib import Path

from agent_backbone.services.telemetry._common import (
    event_id,
    normalize_claude_content,
    parse_timestamp,
    read_jsonl_since,
    simplify_value,
)
from agent_backbone.services.telemetry.interface import TelemetryAdapter
from agent_backbone.services.telemetry.models import (
    TelemetryBatch,
    TelemetryEvent,
    TelemetrySource,
    TelemetrySourceKind,
)
from agent_backbone.services.terminal import TerminalRuntime


class ClaudeTelemetryAdapter(TelemetryAdapter):
    """Adapter for Claude Code transcript JSONL files."""

    runtime = TerminalRuntime.CLAUDE

    def __init__(self, projects_dir: Path | None = None) -> None:
        self._projects_dir = projects_dir or (Path.home() / ".claude" / "projects")

    def discover_sources(
        self,
        *,
        session_name: str,
        cwd: Path,
        entity: str | None = None,
    ) -> list[TelemetrySource]:
        project_key = str(cwd).replace("/", "-")
        project_dir = self._projects_dir / project_key
        if not project_dir.exists():
            return []
        files = sorted(project_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
        return [
            TelemetrySource(
                session=session_name,
                entity=entity,
                runtime=self.runtime,
                source_kind=TelemetrySourceKind.JSONL,
                path=path,
            )
            for path in files
        ]

    def read_since(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        records, next_checkpoint = read_jsonl_since(source.path, checkpoint)
        events: list[TelemetryEvent] = []
        last_event_ts: float | None = None
        for line_offset, record in records:
            normalized = self._normalize_record(source, record, line_offset)
            if normalized:
                events.extend(normalized)
                last_event_ts = normalized[-1].ts
        return TelemetryBatch(
            source=source,
            events=events,
            checkpoint=next_checkpoint,
            last_event_ts=last_event_ts,
        )

    def _normalize_record(
        self,
        source: TelemetrySource,
        record: dict[str, object],
        line_offset: int,
    ) -> list[TelemetryEvent]:
        record_type = str(record.get("type") or "runtime.event")
        ts = parse_timestamp(record.get("timestamp"))
        base_event_id = str(record.get("uuid") or f"{source.path.name}:{line_offset}")
        trace_id = str(record.get("toolUseID") or record.get("sessionId") or "") or None
        parent_trace_id = (
            str(record.get("parentToolUseID") or record.get("parentUuid") or "") or None
        )

        def event(
            name: str,
            *,
            event_id_value: str = base_event_id,
            payload: dict[str, object] | None = None,
            model: str | None = None,
            trace: str | None = trace_id,
            parent_trace: str | None = parent_trace_id,
        ) -> TelemetryEvent:
            return TelemetryEvent(
                session=source.session,
                entity=source.entity,
                event=name,
                ts=ts,
                runtime=self.runtime,
                source_kind=source.source_kind,
                source_ref=source.source_ref,
                source_event_id=event_id_value,
                trace_id=trace,
                parent_trace_id=parent_trace,
                model=model,
                payload=payload,
            )

        if record_type == "progress":
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            hook_event = data.get("hookEvent")
            name = "session.progress"
            if hook_event == "SessionStart":
                name = "session.started"
            elif hook_event == "SessionStop":
                name = "session.stopped"
            return [
                event(
                    name,
                    payload={
                        "progress_type": data.get("type"),
                        "hook_event": hook_event,
                        "hook_name": data.get("hookName"),
                        "command": data.get("command"),
                        "cwd": record.get("cwd"),
                        "version": record.get("version"),
                    },
                )
            ]

        if record_type == "file-history-snapshot":
            snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
            return [
                event(
                    "session.snapshot",
                    payload={
                        "message_id": record.get("messageId"),
                        "snapshot": simplify_value(snapshot),  # type: ignore[arg-type]
                        "is_snapshot_update": record.get("isSnapshotUpdate"),
                    },
                )
            ]

        if record_type == "user":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = normalize_claude_content(message.get("content"))
            events = [
                event(
                    "message.user",
                    payload={"role": message.get("role"), "content": content},
                )
            ]
            for idx, part in enumerate(content):
                if part.get("type") != "tool_result":
                    continue
                tool_trace_id = (
                    str(
                        part.get("tool_use_id")
                        or part.get("toolUseID")
                        or record.get("toolUseID")
                        or ""
                    )
                    or trace_id
                )
                is_error = bool(part.get("is_error")) or bool(record.get("isApiErrorMessage"))
                events.append(
                    event(
                        "tool.error" if is_error else "tool.finished",
                        event_id_value=event_id(base_event_id, f"tool_result:{idx}"),
                        trace=tool_trace_id,
                        parent_trace=parent_trace_id,
                        payload={
                            "tool_use_id": tool_trace_id,
                            "result": simplify_value(part),  # type: ignore[arg-type]
                            "is_error": is_error,
                        },
                    )
                )
            return events

        if record_type == "assistant":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = normalize_claude_content(message.get("content"))
            message_model = str(message.get("model") or "") or None
            events = [
                event(
                    "message.assistant",
                    payload={
                        "role": message.get("role"),
                        "content": content,
                        "stop_reason": message.get("stop_reason"),
                        "request_id": record.get("requestId"),
                    },
                    model=message_model,
                )
            ]
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            if usage:
                events.append(
                    event(
                        "token.usage",
                        event_id_value=event_id(base_event_id, "usage"),
                        model=message_model,
                        payload=simplify_value(usage),  # type: ignore[arg-type]
                    )
                )
            for idx, part in enumerate(content):
                part_type = str(part.get("type") or "")
                if part_type == "tool_use":
                    tool_trace_id = str(part.get("id") or part.get("toolUseID") or "") or trace_id
                    events.append(
                        event(
                            "tool.started",
                            event_id_value=event_id(base_event_id, f"tool_use:{idx}"),
                            trace=tool_trace_id,
                            parent_trace=trace_id,
                            model=message_model,
                            payload={
                                "name": part.get("name"),
                                "input": part.get("input"),
                            },
                        )
                    )
                elif part_type == "thinking":
                    events.append(
                        event(
                            "reasoning",
                            event_id_value=event_id(base_event_id, f"thinking:{idx}"),
                            model=message_model,
                            payload=simplify_value(part),  # type: ignore[arg-type]
                        )
                    )
            return events

        return [
            event(
                "runtime.event",
                payload={
                    "record_type": record_type,
                    "payload": simplify_value(record),  # type: ignore[arg-type]
                },
            )
        ]
