"""Runtime-specific native telemetry adapters."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_backbone.services.telemetry.interface import TelemetryAdapter
from agent_backbone.services.telemetry.models import (
    TelemetryBatch,
    TelemetryEvent,
    TelemetrySource,
    TelemetrySourceKind,
)
from agent_backbone.services.terminal import TerminalRuntime

log = logging.getLogger(__name__)


def _parse_timestamp(value: object) -> float:
    if isinstance(value, int | float):
        timestamp = float(value)
        return timestamp / 1000.0 if timestamp > 10_000_000_000 else timestamp
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        timestamp = float(value)
        return timestamp / 1000.0 if timestamp > 10_000_000_000 else timestamp
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _event_id(base: str, suffix: str) -> str:
    return f"{base}:{suffix}"


def _simplify_value(value: object, *, depth: int = 0) -> object:
    if depth >= 4:
        return str(value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list):
        return [_simplify_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        simplified: dict[str, object] = {}
        for key, item in value.items():
            if key in {"encrypted_content", "base_instructions"}:
                continue
            simplified[str(key)] = _simplify_value(item, depth=depth + 1)
        return simplified
    return str(value)


def _read_jsonl_since(
    path: Path,
    checkpoint: dict[str, object] | None,
) -> tuple[list[tuple[int, dict]], dict[str, object]]:
    offset = int(checkpoint.get("offset", 0)) if checkpoint else 0
    if not path.exists():
        return [], {"offset": 0}

    size = path.stat().st_size
    if offset < 0 or offset > size:
        offset = 0

    records: list[tuple[int, dict]] = []
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            line_offset = handle.tell()
            line = handle.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                log.debug("Skipping malformed telemetry line in %s at offset %s", path, line_offset)
                continue
            if isinstance(payload, dict):
                records.append((line_offset, payload))
        new_offset = handle.tell()
    return records, {"offset": new_offset}


def _read_text_since(
    path: Path,
    checkpoint: dict[str, object] | None,
) -> tuple[str, dict[str, object]]:
    offset = int(checkpoint.get("offset", 0)) if checkpoint else 0
    if not path.exists():
        return "", {"offset": 0}

    size = path.stat().st_size
    if offset < 0 or offset > size:
        offset = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        payload = handle.read()
        new_offset = handle.tell()
    return payload, {"offset": new_offset}


def _normalize_claude_content(content: object) -> list[dict[str, object]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    parts: list[dict[str, object]] = []
    for item in content:
        if isinstance(item, dict):
            parts.append(_simplify_value(item))  # type: ignore[arg-type]
    return parts


def _normalize_codex_content(content: object) -> list[dict[str, object]]:
    if not isinstance(content, list):
        return []
    parts: list[dict[str, object]] = []
    for item in content:
        if isinstance(item, dict):
            parts.append(_simplify_value(item))  # type: ignore[arg-type]
    return parts


def _extract_text_parts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    values: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            values.append(text)
    return values


def _cursor_project_slug(cwd: Path) -> str:
    return str(cwd).lstrip("/").replace("/", "-")


def _mtime(path: Path) -> float:
    return path.stat().st_mtime


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
        records, next_checkpoint = _read_jsonl_since(source.path, checkpoint)
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
        record: dict,
        line_offset: int,
    ) -> list[TelemetryEvent]:
        record_type = str(record.get("type") or "runtime.event")
        ts = _parse_timestamp(record.get("timestamp"))
        base_event_id = str(record.get("uuid") or f"{source.path.name}:{line_offset}")
        trace_id = str(record.get("toolUseID") or record.get("sessionId") or "") or None
        parent_trace_id = (
            str(record.get("parentToolUseID") or record.get("parentUuid") or "") or None
        )

        def event(
            name: str,
            *,
            event_id: str = base_event_id,
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
                source_event_id=event_id,
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
                        "snapshot": _simplify_value(snapshot),
                        "is_snapshot_update": record.get("isSnapshotUpdate"),
                    },
                )
            ]

        if record_type == "user":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = _normalize_claude_content(message.get("content"))
            events = [
                event(
                    "message.user",
                    payload={"role": message.get("role"), "content": content},
                )
            ]
            for idx, part in enumerate(content):
                if part.get("type") != "tool_result":
                    continue
                tool_trace_id = str(
                    part.get("tool_use_id")
                    or part.get("toolUseID")
                    or record.get("toolUseID")
                    or ""
                ) or trace_id
                is_error = bool(part.get("is_error")) or bool(record.get("isApiErrorMessage"))
                events.append(
                    event(
                        "tool.error" if is_error else "tool.finished",
                        event_id=_event_id(base_event_id, f"tool_result:{idx}"),
                        trace=tool_trace_id,
                        parent_trace=parent_trace_id,
                        payload={
                            "tool_use_id": tool_trace_id,
                            "result": _simplify_value(part),
                            "is_error": is_error,
                        },
                    )
                )
            return events

        if record_type == "assistant":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = _normalize_claude_content(message.get("content"))
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
                        event_id=_event_id(base_event_id, "usage"),
                        model=message_model,
                        payload=_simplify_value(usage),  # type: ignore[arg-type]
                    )
                )
            for idx, part in enumerate(content):
                part_type = str(part.get("type") or "")
                if part_type == "tool_use":
                    tool_trace_id = str(part.get("id") or part.get("toolUseID") or "") or trace_id
                    events.append(
                        event(
                            "tool.started",
                            event_id=_event_id(base_event_id, f"tool_use:{idx}"),
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
                            event_id=_event_id(base_event_id, f"thinking:{idx}"),
                            model=message_model,
                            payload=_simplify_value(part),  # type: ignore[arg-type]
                        )
                    )
            return events

        return [
            event(
                "runtime.event",
                payload={
                    "record_type": record_type,
                    "payload": _simplify_value(record),
                },
            )
        ]


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
        records, next_checkpoint = _read_jsonl_since(source.path, checkpoint)
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
        record: dict,
        line_offset: int,
    ) -> list[TelemetryEvent]:
        record_type = str(record.get("type") or "runtime.event")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        ts = _parse_timestamp(record.get("timestamp"))
        base_event_id = str(
            payload.get("call_id")
            or payload.get("turn_id")
            or payload.get("id")
            or record.get("id")
            or f"{source.path.name}:{line_offset}"
        )
        trace_id = str(
            payload.get("turn_id")
            or payload.get("call_id")
            or payload.get("id")
            or ""
        ) or None

        def event(
            name: str,
            *,
            event_id: str = base_event_id,
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
                source_event_id=event_id,
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
                        "sandbox_policy": _simplify_value(payload.get("sandbox_policy")),
                    },
                )
            ]

        if record_type == "event_msg":
            return self._normalize_codex_event_msg(event, payload, payload_type)

        if record_type == "response_item":
            return self._normalize_codex_response_item(event, payload, payload_type)

        if record_type == "function_call":
            return [
                event(
                    "tool.started",
                    payload_data=_simplify_value(payload),  # type: ignore[arg-type]
                )
            ]

        if record_type == "function_call_output":
            is_error = bool(payload.get("is_error"))
            return [
                event(
                    "tool.error" if is_error else "tool.finished",
                    payload_data=_simplify_value(payload),  # type: ignore[arg-type]
                )
            ]

        if record_type in {"compaction", "compacted", "context_compacted"}:
            return [
                event(
                    "session.compacted",
                    payload_data={
                        "record_type": record_type,
                        "payload": _simplify_value(payload),
                    },
                )
            ]

        if record_type == "turn_aborted":
            return [
                event(
                    "turn.aborted",
                    payload_data=_simplify_value(payload),  # type: ignore[arg-type]
                )
            ]

        return [
            event(
                "runtime.event",
                payload_data={
                    "record_type": record_type,
                    "payload": _simplify_value(payload or record),
                },
            )
        ]

    def _normalize_codex_event_msg(
        self,
        factory,
        payload: dict,
        payload_type: str,
    ) -> list[TelemetryEvent]:
        if payload_type == "task_started":
            return [factory("task.started", payload_data=_simplify_value(payload))]  # type: ignore[arg-type]
        if payload_type == "task_complete":
            return [factory("task.completed", payload_data=_simplify_value(payload))]  # type: ignore[arg-type]
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
                        "total": _simplify_value(info.get("total_token_usage")),
                        "last": _simplify_value(info.get("last_token_usage")),
                        "model_context_window": payload.get("model_context_window")
                        or info.get("model_context_window"),
                    },
                )
            ]
        return [factory("runtime.event", payload_data=_simplify_value(payload))]  # type: ignore[arg-type]

    def _normalize_codex_response_item(
        self,
        factory,
        payload: dict,
        payload_type: str,
    ) -> list[TelemetryEvent]:
        if payload_type == "reasoning":
            return [factory("reasoning", payload_data=_simplify_value(payload))]  # type: ignore[arg-type]

        if payload_type == "function_call":
            return [factory("tool.started", payload_data=_simplify_value(payload))]  # type: ignore[arg-type]

        if payload_type == "function_call_output":
            is_error = bool(payload.get("is_error"))
            return [
                factory(
                    "tool.error" if is_error else "tool.finished",
                    payload_data=_simplify_value(payload),  # type: ignore[arg-type]
                )
            ]

        if payload_type == "message" and payload.get("role") == "assistant":
            return [
                factory(
                    "message.assistant",
                    payload_data={
                        "role": payload.get("role"),
                        "content": _normalize_codex_content(payload.get("content")),
                    },
                )
            ]

        return []


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
            for path in sorted(chats_dir.glob("session-*.json"), key=_mtime):
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
                    ts=_parse_timestamp(payload.get("startTime")),
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
            last_event_ts=events[-1].ts if events else _parse_timestamp(last_updated),
        )

    def _normalize_snapshot_message(
        self,
        source: TelemetrySource,
        message: dict,
        payload: dict,
    ) -> list[TelemetryEvent]:
        message_type = str(message.get("type") or "runtime.event")
        ts = _parse_timestamp(message.get("timestamp"))
        base_event_id = str(message.get("id") or f"{source.path.name}:{ts}")
        trace_id = str(payload.get("sessionId") or "") or None

        def event(
            name: str,
            *,
            event_id: str = base_event_id,
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
                source_event_id=event_id,
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
                        "content": _extract_text_parts(message.get("content")),
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
                        event_id=_event_id(base_event_id, "usage"),
                        model=message_model,
                        payload_data=_simplify_value(tokens),  # type: ignore[arg-type]
                    )
                )

            thoughts = message.get("thoughts") if isinstance(message.get("thoughts"), list) else []
            for idx, thought in enumerate(thoughts):
                if not isinstance(thought, dict):
                    continue
                events.append(
                    event(
                        "reasoning",
                        event_id=_event_id(base_event_id, f"thought:{idx}"),
                        event_ts=_parse_timestamp(thought.get("timestamp")) or ts,
                        model=message_model,
                        payload_data=_simplify_value(thought),  # type: ignore[arg-type]
                    )
                )

            tool_calls = (
                message.get("toolCalls") if isinstance(message.get("toolCalls"), list) else []
            )
            for idx, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                tool_trace = str(tool_call.get("id") or "") or trace_id
                tool_ts = _parse_timestamp(tool_call.get("timestamp")) or ts
                status = str(tool_call.get("status") or "")
                is_error = status.lower() not in {"", "success", "completed", "done"}
                events.append(
                    event(
                        "tool.started",
                        event_id=_event_id(base_event_id, f"tool_call:{idx}"),
                        event_ts=tool_ts,
                        trace=tool_trace,
                        payload_data={
                            "name": tool_call.get("name"),
                            "args": _simplify_value(tool_call.get("args")),
                        },
                    )
                )
                if tool_call.get("result") is not None or status:
                    events.append(
                        event(
                            "tool.error" if is_error else "tool.finished",
                            event_id=_event_id(base_event_id, f"tool_result:{idx}"),
                            event_ts=tool_ts,
                            trace=tool_trace,
                            payload_data={
                                "status": status,
                                "result": _simplify_value(tool_call.get("result")),
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
                    "payload": _simplify_value(message),
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
            ts = _parse_timestamp(entry.get("timestamp"))
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


class OpenCodeTelemetryAdapter(TelemetryAdapter):
    """Adapter for OpenCode's SQLite activity store."""

    runtime = TerminalRuntime.OPENCODE

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (Path.home() / ".local" / "share" / "opencode" / "opencode.db")

    def discover_sources(
        self,
        *,
        session_name: str,
        cwd: Path,
        entity: str | None = None,
    ) -> list[TelemetrySource]:
        if not self._db_path.exists():
            return []
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """SELECT id
                   FROM session
                   WHERE directory = ?
                   ORDER BY time_updated ASC""",
                (str(cwd),),
            ).fetchall()

        return [
            TelemetrySource(
                session=session_name,
                entity=entity,
                runtime=self.runtime,
                source_kind=TelemetrySourceKind.SQLITE,
                path=self._db_path,
                metadata={
                    "session_id": row[0],
                    "source_ref": f"{self._db_path}#session:{row[0]}",
                },
            )
            for row in rows
        ]

    def read_since(
        self,
        source: TelemetrySource,
        checkpoint: dict[str, object] | None,
    ) -> TelemetryBatch:
        session_id = source.metadata["session_id"]
        cursor_time = int(checkpoint.get("time_created", 0)) if checkpoint else 0
        cursor_id = str(checkpoint.get("row_id", "")) if checkpoint else ""
        with sqlite3.connect(source.path) as conn:
            session_row = conn.execute(
                """SELECT id, directory, title, time_created, time_updated
                   FROM session
                   WHERE id = ?""",
                (session_id,),
            ).fetchone()
            rows = conn.execute(
                """SELECT *
                   FROM (
                       SELECT
                           'message' AS row_kind,
                           m.id AS row_id,
                           m.id AS message_id,
                           m.time_created,
                           m.data,
                           json_extract(m.data, '$.role') AS message_role
                       FROM message AS m
                       WHERE m.session_id = ?
                       UNION ALL
                       SELECT
                           'part' AS row_kind,
                           p.id AS row_id,
                           p.message_id AS message_id,
                           p.time_created,
                           p.data,
                           json_extract(m.data, '$.role') AS message_role
                       FROM part AS p
                       LEFT JOIN message AS m
                         ON m.id = p.message_id
                       WHERE p.session_id = ?
                   )
                   WHERE time_created > ?
                      OR (time_created = ? AND row_id > ?)
                   ORDER BY time_created ASC, row_id ASC""",
                (session_id, session_id, cursor_time, cursor_time, cursor_id),
            ).fetchall()

        events: list[TelemetryEvent] = []
        if session_row and checkpoint is None:
            events.append(
                TelemetryEvent(
                    session=source.session,
                    entity=source.entity,
                    event="session.started",
                    ts=_parse_timestamp(session_row[3]),
                    runtime=self.runtime,
                    source_kind=source.source_kind,
                    source_ref=source.source_ref,
                    source_event_id=f"{session_id}:session",
                    trace_id=session_id,
                    payload={
                        "session_id": session_id,
                        "directory": session_row[1],
                        "title": session_row[2],
                    },
                )
            )

        last_cursor = {"time_created": cursor_time, "row_id": cursor_id}
        last_event_ts = events[-1].ts if events else None
        for row_kind, row_id, message_id, time_created, raw_data, message_role in rows:
            try:
                payload = json.loads(raw_data)
            except (TypeError, json.JSONDecodeError):
                payload = {"raw": raw_data}

            if row_kind == "message":
                normalized = self._normalize_message(
                    source,
                    payload,
                    row_id=str(row_id),
                    message_id=str(message_id),
                    ts=_parse_timestamp(time_created),
                )
            else:
                normalized = self._normalize_part(
                    source,
                    payload,
                    row_id=str(row_id),
                    message_id=str(message_id),
                    message_role=str(message_role or ""),
                    ts=_parse_timestamp(time_created),
                )

            if normalized:
                events.extend(normalized)
                last_event_ts = normalized[-1].ts
            last_cursor = {"time_created": int(time_created), "row_id": str(row_id)}

        return TelemetryBatch(
            source=source,
            events=events,
            checkpoint=last_cursor,
            last_event_ts=last_event_ts,
        )

    def _normalize_message(
        self,
        source: TelemetrySource,
        payload: dict,
        *,
        row_id: str,
        message_id: str,
        ts: float,
    ) -> list[TelemetryEvent]:
        role = str(payload.get("role") or "")
        trace_id = message_id or row_id
        model = self._message_model(payload)

        def event(
            name: str,
            *,
            event_id: str = row_id,
            payload_data: dict[str, object] | None = None,
        ) -> TelemetryEvent:
            return TelemetryEvent(
                session=source.session,
                entity=source.entity,
                event=name,
                ts=ts,
                runtime=self.runtime,
                source_kind=source.source_kind,
                source_ref=source.source_ref,
                source_event_id=event_id,
                trace_id=trace_id,
                parent_trace_id=str(payload.get("parentID") or "") or None,
                model=model,
                payload=payload_data,
            )

        if role == "user":
            return [
                event(
                    "message.user",
                    payload_data={
                        "summary": _simplify_value(payload.get("summary")),
                        "agent": payload.get("agent"),
                    },
                )
            ]
        if role == "assistant":
            return [
                event(
                    "message.assistant",
                    payload_data={
                        "agent": payload.get("agent"),
                        "mode": payload.get("mode"),
                        "finish": payload.get("finish"),
                        "cost": payload.get("cost"),
                    },
                )
            ]
        return [
            event(
                "runtime.event",
                payload_data=_simplify_value(payload),  # type: ignore[arg-type]
            )
        ]

    def _normalize_part(
        self,
        source: TelemetrySource,
        payload: dict,
        *,
        row_id: str,
        message_id: str,
        message_role: str,
        ts: float,
    ) -> list[TelemetryEvent]:
        part_type = str(payload.get("type") or "runtime.event")
        trace_id = message_id or row_id

        def event(
            name: str,
            *,
            event_id: str = row_id,
            payload_data: dict[str, object] | None = None,
        ) -> TelemetryEvent:
            return TelemetryEvent(
                session=source.session,
                entity=source.entity,
                event=name,
                ts=ts,
                runtime=self.runtime,
                source_kind=source.source_kind,
                source_ref=source.source_ref,
                source_event_id=event_id,
                trace_id=trace_id,
                payload=payload_data,
            )

        if part_type == "text":
            event_name = "message.user" if message_role == "user" else "message.assistant"
            return [
                event(
                    event_name,
                    payload_data={"text": payload.get("text")},
                )
            ]

        if part_type == "reasoning":
            return [
                event(
                    "reasoning",
                    payload_data={"text": payload.get("text")},
                )
            ]

        if part_type == "step-start":
            return [
                event(
                    "task.started",
                    payload_data={"snapshot": payload.get("snapshot")},
                )
            ]

        if part_type == "step-finish":
            events = [
                event(
                    "task.completed",
                    payload_data={
                        "reason": payload.get("reason"),
                        "snapshot": payload.get("snapshot"),
                    },
                )
            ]
            tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
            if tokens:
                events.append(
                    event(
                        "token.usage",
                        event_id=_event_id(row_id, "usage"),
                        payload_data=_simplify_value(tokens),  # type: ignore[arg-type]
                    )
                )
            return events

        return [
            event(
                "runtime.event",
                payload_data=_simplify_value(payload),  # type: ignore[arg-type]
            )
        ]

    def _message_model(self, payload: dict) -> str | None:
        model_info = payload.get("model") if isinstance(payload.get("model"), dict) else {}
        provider = str(payload.get("providerID") or model_info.get("providerID") or "")
        model = str(payload.get("modelID") or model_info.get("modelID") or "")
        combined = ":".join(part for part in (provider, model) if part)
        return combined or None


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
        project_dir = self._projects_dir / _cursor_project_slug(cwd)
        if not project_dir.exists():
            return []

        sources: list[TelemetrySource] = []
        for path in sorted((project_dir / "agent-transcripts").rglob("*.jsonl"), key=_mtime):
            sources.append(
                TelemetrySource(
                    session=session_name,
                    entity=entity,
                    runtime=self.runtime,
                    source_kind=TelemetrySourceKind.JSONL,
                    path=path,
                )
            )
        for path in sorted((project_dir / "agent-transcripts").glob("*.txt"), key=_mtime):
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
        for path in sorted((project_dir / "agent-tools").glob("*.txt"), key=_mtime):
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
        for path in sorted((project_dir / "terminals").glob("*.txt"), key=_mtime):
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
        records, next_checkpoint = _read_jsonl_since(source.path, checkpoint)
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
                        "content": _extract_text_parts(content),
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
        delta, next_checkpoint = _read_text_since(source.path, checkpoint)
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


_ADAPTERS: dict[TerminalRuntime, TelemetryAdapter] = {
    TerminalRuntime.CLAUDE: ClaudeTelemetryAdapter(),
    TerminalRuntime.CODEX: CodexTelemetryAdapter(),
    TerminalRuntime.GEMINI: GeminiTelemetryAdapter(),
    TerminalRuntime.OPENCODE: OpenCodeTelemetryAdapter(),
    TerminalRuntime.CURSOR: CursorTelemetryAdapter(),
}


def get_telemetry_adapter(runtime: TerminalRuntime | str) -> TelemetryAdapter | None:
    """Return the adapter for a supported runtime, if any."""
    try:
        normalized = runtime if isinstance(runtime, TerminalRuntime) else TerminalRuntime(runtime)
    except ValueError:
        return None
    return _ADAPTERS.get(normalized)
