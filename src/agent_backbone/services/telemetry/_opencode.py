"""OpenCode telemetry adapter."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_backbone.services.telemetry._common import event_id, parse_timestamp, simplify_value
from agent_backbone.services.telemetry.interface import TelemetryAdapter
from agent_backbone.services.telemetry.models import (
    TelemetryBatch,
    TelemetryEvent,
    TelemetrySource,
    TelemetrySourceKind,
)
from agent_backbone.services.terminal import TerminalRuntime


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
                    ts=parse_timestamp(session_row[3]),
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
                    ts=parse_timestamp(time_created),
                )
            else:
                normalized = self._normalize_part(
                    source,
                    payload,
                    row_id=str(row_id),
                    message_id=str(message_id),
                    message_role=str(message_role or ""),
                    ts=parse_timestamp(time_created),
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
        payload: dict[str, object],
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
            event_id_value: str = row_id,
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
                source_event_id=event_id_value,
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
                        "summary": simplify_value(payload.get("summary")),
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
                payload_data=simplify_value(payload),  # type: ignore[arg-type]
            )
        ]

    def _normalize_part(
        self,
        source: TelemetrySource,
        payload: dict[str, object],
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
            event_id_value: str = row_id,
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
                source_event_id=event_id_value,
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
                        event_id_value=event_id(row_id, "usage"),
                        payload_data=simplify_value(tokens),  # type: ignore[arg-type]
                    )
                )
            return events

        return [
            event(
                "runtime.event",
                payload_data=simplify_value(payload),  # type: ignore[arg-type]
            )
        ]

    def _message_model(self, payload: dict[str, object]) -> str | None:
        model_info = payload.get("model") if isinstance(payload.get("model"), dict) else {}
        provider = str(payload.get("providerID") or model_info.get("providerID") or "")
        model = str(payload.get("modelID") or model_info.get("modelID") or "")
        combined = ":".join(part for part in (provider, model) if part)
        return combined or None
