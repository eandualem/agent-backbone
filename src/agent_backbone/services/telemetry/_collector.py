"""Incremental collection of CLI-native telemetry."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.telemetry._adapters import get_telemetry_adapter
from agent_backbone.services.telemetry.models import CollectionSummary, TelemetryEvent
from agent_backbone.services.terminal import (
    TerminalRuntime,
    query_format_vars,
    resolve_agent_dir,
    resolve_terminal_runtime,
)

log = logging.getLogger(__name__)

_PANE_PATH_FORMAT = "pane_current_path=#{pane_current_path}"
_SKIPPED_RUNTIMES = frozenset({TerminalRuntime.UNKNOWN, TerminalRuntime.SHELL})


def _ts_string(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.6f}"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _event_row(event: TelemetryEvent) -> dict[str, str | None]:
    return {
        "session": event.session,
        "event": event.event,
        "entity": event.entity,
        "runtime": event.runtime.value,
        "source_kind": event.source_kind.value,
        "source_ref": event.source_ref,
        "source_event_id": event.source_event_id,
        "trace_id": event.trace_id,
        "parent_trace_id": event.parent_trace_id,
        "model": event.model,
        "data": json.dumps(event.payload, sort_keys=True) if event.payload is not None else None,
        "ts": _ts_string(event.ts),
        "received_at": _now_iso(),
    }


async def collect_session_telemetry(
    *,
    db: BackboneDB,
    session_name: str,
    runtime: TerminalRuntime | str,
    cwd: str | Path,
    entity: str | None = None,
    max_sources: int | None = None,
    claimed_sources: set[str] | None = None,
) -> CollectionSummary:
    """Collect new telemetry events for a single session/runtime."""
    adapter = get_telemetry_adapter(runtime)
    if isinstance(runtime, TerminalRuntime):
        normalized_runtime = runtime
    else:
        try:
            normalized_runtime = TerminalRuntime(runtime)
        except ValueError:
            normalized_runtime = TerminalRuntime.UNKNOWN
    if adapter is None:
        return CollectionSummary(
            runtime=normalized_runtime,
            sources_scanned=0,
            events_recorded=0,
            updated_sources=[],
        )

    sources = adapter.discover_sources(
        session_name=session_name,
        cwd=Path(cwd),
        entity=entity,
    )
    # In shared-worktree scenarios (swarms), multiple sessions share a cwd
    # and discover the same source files. Claim sources on first encounter
    # so each file is ingested by at most one session per collection cycle.
    if claimed_sources is not None:
        sources = [s for s in sources if s.source_ref not in claimed_sources]
        for s in sources:
            claimed_sources.add(s.source_ref)
    if max_sources is not None:
        sources = sources[-max_sources:]

    events_recorded = 0
    updated_sources: list[str] = []
    for source in sources:
        existing = await db.get_telemetry_checkpoint(session_name, source.source_ref)
        checkpoint = existing["checkpoint"] if existing else None
        batch = adapter.read_since(source, checkpoint)
        if batch.events:
            rows = [_event_row(item) for item in batch.events]
            events_recorded += await db.record_activity_batch(rows)
            updated_sources.append(source.source_ref)
        await db.upsert_telemetry_checkpoint(
            session=session_name,
            source_ref=source.source_ref,
            runtime=source.runtime.value,
            source_kind=source.source_kind.value,
            checkpoint=batch.checkpoint,
            entity=entity,
            last_event_ts=_ts_string(batch.last_event_ts),
        )

    return CollectionSummary(
        runtime=normalized_runtime,
        sources_scanned=len(sources),
        events_recorded=events_recorded,
        updated_sources=updated_sources,
    )


async def collect_active_session_telemetry(
    *,
    config: BackboneConfig,
    db: BackboneDB,
    active_sessions: set[str],
    max_sources_per_session: int = 5,
) -> dict[str, CollectionSummary]:
    """Collect telemetry for all active non-service sessions with supported runtimes."""
    summaries: dict[str, CollectionSummary] = {}
    # Track which source files are already claimed by a session in this cycle
    # to prevent cross-contamination in shared-worktree scenarios (swarms).
    claimed_sources: set[str] = set()
    for session_name in sorted(active_sessions):
        if session_name in config.entities.service_sessions:
            continue

        cwd = await _session_cwd(config, session_name)
        if not cwd:
            continue

        runtime = await resolve_terminal_runtime(session_name)
        if runtime in _SKIPPED_RUNTIMES:
            continue

        entity = config.registry.entity_by_session.get(session_name, session_name)
        try:
            summary = await collect_session_telemetry(
                db=db,
                session_name=session_name,
                runtime=runtime,
                cwd=cwd,
                entity=entity,
                max_sources=max_sources_per_session,
                claimed_sources=claimed_sources,
            )
        except Exception:
            log.exception("Telemetry collection failed for session '%s'", session_name)
            continue

        summaries[session_name] = summary
        if summary.events_recorded:
            log.info(
                "Collected %s telemetry events for '%s' (%s sources)",
                summary.events_recorded,
                session_name,
                summary.sources_scanned,
            )
    return summaries


async def _session_cwd(config: BackboneConfig, session_name: str) -> str:
    """Resolve the effective cwd for an active tmux session."""
    tmux_vars = await query_format_vars(session_name, _PANE_PATH_FORMAT)
    pane_cwd = tmux_vars.get("pane_current_path", "").strip()
    if pane_cwd:
        return pane_cwd
    return resolve_agent_dir(session_name, config.registry)
