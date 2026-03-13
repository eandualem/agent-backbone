"""Codex telemetry adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from agent_backbone.services.telemetry._common import (
    normalize_codex_content,
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

_EventFactory = Callable[..., TelemetryEvent]


class CodexTelemetryAdapter(TelemetryAdapter):
    """Adapter for Codex session JSONL logs."""

    runtime = TerminalRuntime.CODEX

    def __init__(self, sessions_dir: Path | None = None) -> None:
        self._sessions_dir = sessions_dir or (Path.home() / ".codex" / "sessions")

    def discover_sources(
        self,
        *,
        session_name: str,
        cwd: Path,
        entity: str | None = None,
    ) -> list[TelemetrySource]:
        if not self._sessions_dir.exists():
            return []
        matched: list[Path] = []
        for path in sorted(
            self._sessions_dir.rglob("*.jsonl"),
            key=lambda item: item.stat().st_mtime,
        ):
            if self._session_cwd(path) == str(cwd):
                matched.append(path)
        return [
            TelemetrySource(
                session=session_name,
                entity=entity,
                runtime=self.runtime,
                source_kind=TelemetrySourceKind.JSONL,
                path=path,
            )
            for path in matched
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

    def _session_cwd(self, path: Path) -> str | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for _ in range(5):
                    line = handle.readline()
                    if not line:
                        break
                    payload = json.loads(line)
                    if payload.get("type") == "session_meta":
                        meta = (
                            payload.get("payload")
                            if isinstance(payload.get("payload"), dict)
                            else {}
                        )
                        cwd = meta.get("cwd")
                        return str(cwd) if cwd else None
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def _normalize_record(
        self,
        source: TelemetrySource,
        record: dict[str, object],
        line_offset: int,
    ) -> list[TelemetryEvent]:
        record_type = str(record.get("type") or "runtime.event")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        ts = parse_timestamp(record.get("timestamp"))
        base_event_id = str(
            payload.get("call_id")
            or payload.get("turn_id")
            or payload.get("id")
            or record.get("id")
            or f"{source.path.name}:{line_offset}"
        )
        trace_id = (
            str(payload.get("turn_id") or payload.get("call_id") or payload.get("id") or "") or None
        )

        def event(
            name: str,
            *,
            event_id_value: str = base_event_id,
            payload_data: dict[str, object] | None = None,
            model: str | None = None,
            trace: str | None = trace_id,
            parent_trace: str | None = None,
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
                payload=payload_data,
            )

        if record_type == "session_meta":
            meta = payload
            return [
                event(
                    "session.started",
                    payload_data={
                        "session_id": meta.get("id"),
                        "cwd": meta.get("cwd"),
                        "originator": meta.get("originator"),
                        "cli_version": meta.get("cli_version"),
                        "source": meta.get("source"),
                        "model_provider": meta.get("model_provider"),
                    },
                    trace=str(meta.get("id") or "") or None,
                )
            ]

        if record_type == "turn_context":
            return [
                event(
                    "session.context",
                    payload_data={
                        "turn_id": payload.get("turn_id"),
                        "cwd": payload.get("cwd"),
                        "model": payload.get("model"),
                        "approval_policy": payload.get("approval_policy"),
                        "sandbox_policy": simplify_value(payload.get("sandbox_policy")),
                    },
                )
            ]

        if record_type == "event_msg":
            return self._normalize_event_message(event, payload, payload_type)

        if record_type == "response_item":
            return self._normalize_response_item(event, payload, payload_type)

        if record_type == "function_call":
            return [
                event(
                    "tool.started",
                    payload_data=simplify_value(payload),  # type: ignore[arg-type]
                )
            ]

        if record_type == "function_call_output":
            is_error = bool(payload.get("is_error"))
            return [
                event(
                    "tool.error" if is_error else "tool.finished",
                    payload_data=simplify_value(payload),  # type: ignore[arg-type]
                )
            ]

        if record_type in {"compaction", "compacted", "context_compacted"}:
            return [
                event(
                    "session.compacted",
                    payload_data={
                        "record_type": record_type,
                        "payload": simplify_value(payload),
                    },
                )
            ]

        if record_type == "turn_aborted":
            return [
                event(
                    "turn.aborted",
                    payload_data=simplify_value(payload),  # type: ignore[arg-type]
                )
            ]

        return [
            event(
                "runtime.event",
                payload_data={
                    "record_type": record_type,
                    "payload": simplify_value(payload or record),
                },
            )
        ]

    def _normalize_event_message(
        self,
        factory: _EventFactory,
        payload: dict[str, object],
        payload_type: str,
    ) -> list[TelemetryEvent]:
        if payload_type == "task_started":
            return [factory("task.started", payload_data=simplify_value(payload))]  # type: ignore[arg-type]
        if payload_type == "task_complete":
            return [factory("task.completed", payload_data=simplify_value(payload))]  # type: ignore[arg-type]
        if payload_type == "user_message":
            return [
                factory(
                    "message.user",
                    payload_data={
                        "message": payload.get("message"),
                        "images": payload.get("images"),
                    },
                )
            ]
        if payload_type == "agent_message":
            return [
                factory(
                    "message.assistant",
                    payload_data={
                        "message": payload.get("message"),
                        "phase": payload.get("phase"),
                    },
                )
            ]
        if payload_type == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            return [
                factory(
                    "token.usage",
                    payload_data={
                        "total": simplify_value(info.get("total_token_usage")),
                        "last": simplify_value(info.get("last_token_usage")),
                        "model_context_window": payload.get("model_context_window")
                        or info.get("model_context_window"),
                    },
                )
            ]
        return [factory("runtime.event", payload_data=simplify_value(payload))]  # type: ignore[arg-type]

    def _normalize_response_item(
        self,
        factory: _EventFactory,
        payload: dict[str, object],
        payload_type: str,
    ) -> list[TelemetryEvent]:
        if payload_type == "reasoning":
            return [factory("reasoning", payload_data=simplify_value(payload))]  # type: ignore[arg-type]

        if payload_type == "function_call":
            return [factory("tool.started", payload_data=simplify_value(payload))]  # type: ignore[arg-type]

        if payload_type == "function_call_output":
            is_error = bool(payload.get("is_error"))
            return [
                factory(
                    "tool.error" if is_error else "tool.finished",
                    payload_data=simplify_value(payload),  # type: ignore[arg-type]
                )
            ]

        if payload_type == "message" and payload.get("role") == "assistant":
            return [
                factory(
                    "message.assistant",
                    payload_data={
                        "role": payload.get("role"),
                        "content": normalize_codex_content(payload.get("content")),
                    },
                )
            ]

        return []
