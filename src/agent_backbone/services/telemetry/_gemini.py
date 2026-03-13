"""Gemini telemetry adapter."""

from __future__ import annotations

import json
from pathlib import Path

from agent_backbone.services.telemetry._common import (
    event_id,
    extract_text_parts,
    mtime,
    parse_timestamp,
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


class GeminiTelemetryAdapter(TelemetryAdapter):
    """Adapter for Gemini CLI project snapshots and logs."""

    runtime = TerminalRuntime.GEMINI

    def __init__(self, tmp_dir: Path | None = None) -> None:
        self._tmp_dir = tmp_dir or (Path.home() / ".gemini" / "tmp")

    def discover_sources(
        self,
        *,
        session_name: str,
        cwd: Path,
        entity: str | None = None,
    ) -> list[TelemetrySource]:
        project_dir = self._tmp_dir / cwd.name
        if not project_dir.exists():
            return []

        sources: list[TelemetrySource] = []
        chats_dir = project_dir / "chats"
        if chats_dir.exists():
            for path in sorted(chats_dir.glob("session-*.json"), key=mtime):
                sources.append(
                    TelemetrySource(
                        session=session_name,
                        entity=entity,
                        runtime=self.runtime,
                        source_kind=TelemetrySourceKind.SNAPSHOT,
                        path=path,
                    )
                )

        logs_path = project_dir / "logs.json"
        if logs_path.exists():
            sources.append(
                TelemetrySource(
                    session=session_name,
                    entity=entity,
                    runtime=self.runtime,
                    source_kind=TelemetrySourceKind.LOG,
                    path=logs_path,
                )
            )
        return sources

    def read_since(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        if source.path.name == "logs.json":
            return self._read_logs(source, checkpoint)
        return self._read_snapshot(source, checkpoint)

    def _read_snapshot(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        if not source.path.exists():
            return TelemetryBatch(source=source, events=[], checkpoint={"message_count": 0})

        payload = json.loads(source.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return TelemetryBatch(source=source, events=[], checkpoint={"message_count": 0})

        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        previous_count = int(checkpoint.get("message_count", 0)) if checkpoint else 0
        start_index = previous_count if previous_count <= len(messages) else 0

        events: list[TelemetryEvent] = []
        if checkpoint is None:
            events.append(
                TelemetryEvent(
                    session=source.session,
                    entity=source.entity,
                    event="session.started",
                    ts=parse_timestamp(payload.get("startTime")),
                    runtime=self.runtime,
                    source_kind=source.source_kind,
                    source_ref=source.source_ref,
                    source_event_id=f"{payload.get('sessionId')}:session",
                    trace_id=str(payload.get("sessionId") or "") or None,
                    payload={
                        "session_id": payload.get("sessionId"),
                        "project_hash": payload.get("projectHash"),
                        "kind": payload.get("kind"),
                    },
                )
            )

        for message in messages[start_index:]:
            if not isinstance(message, dict):
                continue
            events.extend(self._normalize_snapshot_message(source, message, payload))

        last_updated = payload.get("lastUpdated")
        return TelemetryBatch(
            source=source,
            events=events,
            checkpoint={
                "message_count": len(messages),
                "last_updated": last_updated,
            },
            last_event_ts=events[-1].ts if events else parse_timestamp(last_updated),
        )

    def _normalize_snapshot_message(
        self,
        source: TelemetrySource,
        message: dict[str, object],
        payload: dict[str, object],
    ) -> list[TelemetryEvent]:
        message_type = str(message.get("type") or "runtime.event")
        ts = parse_timestamp(message.get("timestamp"))
        base_event_id = str(message.get("id") or f"{source.path.name}:{ts}")
        trace_id = str(payload.get("sessionId") or "") or None

        def event(
            name: str,
            *,
            event_id_value: str = base_event_id,
            event_ts: float | None = None,
            payload_data: dict[str, object] | None = None,
            model: str | None = None,
            trace: str | None = trace_id,
            parent_trace: str | None = None,
        ) -> TelemetryEvent:
            return TelemetryEvent(
                session=source.session,
                entity=source.entity,
                event=name,
                ts=ts if event_ts is None else event_ts,
                runtime=self.runtime,
                source_kind=source.source_kind,
                source_ref=source.source_ref,
                source_event_id=event_id_value,
                trace_id=trace,
                parent_trace_id=parent_trace,
                model=model,
                payload=payload_data,
            )

        if message_type == "user":
            return [
                event(
                    "message.user",
                    payload_data={
                        "content": extract_text_parts(message.get("content")),
                    },
                )
            ]

        if message_type == "gemini":
            message_model = str(message.get("model") or "") or None
            events = [
                event(
                    "message.assistant",
                    payload_data={
                        "content": message.get("content"),
                        "kind": payload.get("kind"),
                    },
                    model=message_model,
                )
            ]

            tokens = message.get("tokens") if isinstance(message.get("tokens"), dict) else {}
            if tokens:
                events.append(
                    event(
                        "token.usage",
                        event_id_value=event_id(base_event_id, "usage"),
                        model=message_model,
                        payload_data=simplify_value(tokens),  # type: ignore[arg-type]
                    )
                )

            thoughts = message.get("thoughts") if isinstance(message.get("thoughts"), list) else []
            for idx, thought in enumerate(thoughts):
                if not isinstance(thought, dict):
                    continue
                events.append(
                    event(
                        "reasoning",
                        event_id_value=event_id(base_event_id, f"thought:{idx}"),
                        event_ts=parse_timestamp(thought.get("timestamp")) or ts,
                        model=message_model,
                        payload_data=simplify_value(thought),  # type: ignore[arg-type]
                    )
                )

            tool_calls = (
                message.get("toolCalls") if isinstance(message.get("toolCalls"), list) else []
            )
            for idx, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                tool_trace = str(tool_call.get("id") or "") or trace_id
                tool_ts = parse_timestamp(tool_call.get("timestamp")) or ts
                status = str(tool_call.get("status") or "")
                is_error = status.lower() not in {"", "success", "completed", "done"}
                events.append(
                    event(
                        "tool.started",
                        event_id_value=event_id(base_event_id, f"tool_call:{idx}"),
                        event_ts=tool_ts,
                        trace=tool_trace,
                        payload_data={
                            "name": tool_call.get("name"),
                            "args": simplify_value(tool_call.get("args")),
                        },
                    )
                )
                if tool_call.get("result") is not None or status:
                    events.append(
                        event(
                            "tool.error" if is_error else "tool.finished",
                            event_id_value=event_id(base_event_id, f"tool_result:{idx}"),
                            event_ts=tool_ts,
                            trace=tool_trace,
                            payload_data={
                                "status": status,
                                "result": simplify_value(tool_call.get("result")),
                                "display": tool_call.get("resultDisplay"),
                            },
                        )
                    )
            return events

        if message_type == "error":
            return [
                event(
                    "runtime.error",
                    payload_data={"content": message.get("content")},
                )
            ]

        if message_type == "info":
            return [
                event(
                    "runtime.event",
                    payload_data={
                        "message_type": message_type,
                        "content": message.get("content"),
                    },
                )
            ]

        return [
            event(
                "runtime.event",
                payload_data={
                    "message_type": message_type,
                    "payload": simplify_value(message),  # type: ignore[arg-type]
                },
            )
        ]

    def _read_logs(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        if not source.path.exists():
            return TelemetryBatch(source=source, events=[], checkpoint={"entry_count": 0})

        payload = json.loads(source.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return TelemetryBatch(source=source, events=[], checkpoint={"entry_count": 0})

        previous_count = int(checkpoint.get("entry_count", 0)) if checkpoint else 0
        start_index = previous_count if previous_count <= len(payload) else 0
        events: list[TelemetryEvent] = []
        for idx, entry in enumerate(payload[start_index:], start=start_index):
            if not isinstance(entry, dict):
                continue
            ts = parse_timestamp(entry.get("timestamp"))
            events.append(
                TelemetryEvent(
                    session=source.session,
                    entity=source.entity,
                    event="runtime.event",
                    ts=ts,
                    runtime=self.runtime,
                    source_kind=source.source_kind,
                    source_ref=source.source_ref,
                    source_event_id=str(entry.get("messageId") or f"log:{idx}"),
                    trace_id=str(entry.get("sessionId") or "") or None,
                    payload={
                        "entry_type": entry.get("type"),
                        "message": entry.get("message"),
                    },
                )
            )

        return TelemetryBatch(
            source=source,
            events=events,
            checkpoint={"entry_count": len(payload)},
            last_event_ts=events[-1].ts if events else None,
        )
