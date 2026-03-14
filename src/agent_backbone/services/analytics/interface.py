"""Analytics service — Python extractors over raw agent_activity rows.

All aggregation happens in Python over normalized columns + data JSON.
No SQL json_extract. Missing/malformed data must not crash analytics.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token bucket normalization
# ---------------------------------------------------------------------------

# Canonical bucket names for normalized output
_TOKEN_BUCKETS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# Runtime-specific aliases → canonical bucket name
_TOKEN_ALIASES: dict[str, str] = {
    # Claude / generic
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
    # Gemini
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "cached_content_token_count": "cache_read_tokens",
    # Codex / OpenAI
    "prompt_token_count": "input_tokens",
    "completion_token_count": "output_tokens",
}


def _safe_json(data_str: str | None) -> dict:
    """Parse data JSON, returning empty dict on failure."""
    if not data_str:
        return {}
    try:
        parsed = json.loads(data_str)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_tokens_from_flat(source: dict, tokens: dict[str, int]) -> None:
    """Extract token counts from a flat dict into tokens accumulator."""
    for raw_key, canonical in _TOKEN_ALIASES.items():
        val = source.get(raw_key)
        if val is not None:
            try:
                tokens[canonical] = tokens.get(canonical, 0) + int(val)
            except (ValueError, TypeError):
                pass


def _extract_tokens(data: dict) -> dict[str, int]:
    """Extract normalized token counts from a data payload.

    Handles flat keys (Claude/Gemini) and Codex nested
    ``{"total": {"input_tokens": ...}, "last": {...}}`` shapes.
    When a ``total`` sub-dict is present its values take priority.
    """
    tokens: dict[str, int] = {}

    # Check for Codex nested structure first
    total_sub = data.get("total")
    if isinstance(total_sub, dict) and total_sub:
        _extract_tokens_from_flat(total_sub, tokens)
        # If we got tokens from nested, return early — don't double-count
        if tokens:
            return tokens

    # Flat keys (Claude, Gemini, OpenAI, or any direct posting)
    _extract_tokens_from_flat(data, tokens)
    return tokens


def _extract_cost(data: dict) -> float | None:
    """Extract cost_usd from data if present."""
    for key in ("cost_usd", "cost", "total_cost"):
        val = data.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


def _extract_tool_name(data: dict) -> str:
    """Extract tool name from a tool event's data, with unknown fallback."""
    for key in ("tool_name", "tool", "name", "function_name"):
        val = data.get(key)
        if val and isinstance(val, str):
            return val
    return "unknown"


def _extract_error_info(data: dict) -> dict:
    """Extract error details from an error event."""
    return {
        "error_type": data.get("error_type", data.get("type", "unknown")),
        "message": data.get("message", data.get("error", "")),
        "tool_name": data.get("tool_name", data.get("tool")),
    }


def _extract_duration(data: dict) -> float | None:
    """Extract duration_ms or duration_seconds from data."""
    for key in ("duration_ms",):
        val = data.get(key)
        if val is not None:
            try:
                return float(val) / 1000.0
            except (ValueError, TypeError):
                pass
    for key in ("duration_seconds", "duration", "elapsed"):
        val = data.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _dedup_key(row: dict) -> tuple | None:
    """Build dedup key from (session, event, source_ref, source_event_id).

    Returns None if source IDs are missing — row cannot be deduped.
    """
    source_ref = row.get("source_ref")
    source_event_id = row.get("source_event_id")
    if not source_ref and not source_event_id:
        return None
    return (row["session"], row["event"], source_ref, source_event_id)


def deduplicate_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Best-effort dedup replayed events.

    Returns (deduped_rows, original_count).
    """
    original_count = len(rows)
    seen: set[tuple] = set()
    result: list[dict] = []
    for row in rows:
        key = _dedup_key(row)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        result.append(row)
    return result, original_count


# ---------------------------------------------------------------------------
# Swarm attribution
# ---------------------------------------------------------------------------


@dataclass
class SwarmAttribution:
    """Swarm context for a session."""

    is_swarm_worker: bool = False
    swarm_id: str | None = None
    swarm_worker_name: str | None = None
    swarm_worker_role: str | None = None
    coding_agent_session: str | None = None
    repo: str | None = None
    task_id: str | None = None


def _apply_swarm_attribution(entry: dict, sa: SwarmAttribution) -> None:
    """Add swarm attribution fields to an output dict."""
    if sa.is_swarm_worker:
        entry["is_swarm_worker"] = True
        entry["swarm_id"] = sa.swarm_id
        entry["swarm_worker_name"] = sa.swarm_worker_name
        entry["swarm_worker_role"] = sa.swarm_worker_role
        entry["coding_agent_session"] = sa.coding_agent_session
        entry["repo"] = sa.repo
        entry["task_id"] = sa.task_id
    else:
        entry["is_swarm_worker"] = False


# ---------------------------------------------------------------------------
# Aggregation results
# ---------------------------------------------------------------------------


@dataclass
class SessionBreakdown:
    """Per-session aggregation within an overview."""

    session: str
    event_count: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    has_cost: bool = False
    error_count: int = 0
    tool_count: int = 0
    swarm: SwarmAttribution = field(default_factory=SwarmAttribution)


@dataclass
class OverviewTotals:
    """Aggregate totals across all sessions."""

    tokens: dict[str, int] = field(default_factory=dict)
    captured_cost_usd: float = 0.0
    cost_status: str = "unavailable"  # "partial" | "unavailable"
    errors: dict[str, int] = field(default_factory=dict)  # category → count
    tool_count: int = 0
    observed_window_seconds: float = 0.0
    session_seconds: float = 0.0
    task_seconds: float = 0.0
    tool_seconds: float = 0.0
    duration_is_best_effort: bool = True


@dataclass
class ToolStat:
    """Aggregated stats for one tool."""

    tool_name: str
    call_count: int = 0
    error_count: int = 0
    total_duration_seconds: float = 0.0


@dataclass
class ErrorEntry:
    """One error from the error feed."""

    session: str
    event: str
    ts: str
    error_type: str = "unknown"
    message: str = ""
    tool_name: str | None = None
    swarm: SwarmAttribution = field(default_factory=SwarmAttribution)


# ---------------------------------------------------------------------------
# Analytics Service
# ---------------------------------------------------------------------------


class AnalyticsService:
    """Stateless analytics over agent_activity data.

    All methods receive a BackboneDB and query parameters.
    Extraction and aggregation happen in Python.
    """

    # -- Event category sets --
    _ERROR_EVENTS = frozenset({
        "tool.error", "runtime.error", "turn.aborted",
    })
    _TOOL_EVENTS = frozenset({"tool.started", "tool.finished"})
    _TOKEN_EVENTS = frozenset({"token.usage"})
    # Events that may carry cost_usd even though they aren't token.usage
    _COST_BEARING_EVENTS = frozenset({
        "token.usage", "message.assistant",
    })

    async def resolve_sessions(
        self,
        db: BackboneDB,
        session: str,
        *,
        include_swarm_workers: bool = True,
        swarm_id: str | None = None,
        worker_name: str | None = None,
        worker_role: str | None = None,
    ) -> tuple[list[str], dict[str, SwarmAttribution]]:
        """Resolve the full set of sessions to query, including swarm workers.

        Returns (session_list, attribution_map).
        """
        sessions = [session]

        # Check if the queried session itself is a swarm worker
        self_attr = await db.get_worker_swarm_attribution(session)
        if self_attr is not None:
            attr_map: dict[str, SwarmAttribution] = {
                session: SwarmAttribution(
                    is_swarm_worker=True,
                    swarm_id=self_attr["swarm_id"],
                    swarm_worker_name=self_attr["worker_name"],
                    swarm_worker_role=self_attr["worker_role"],
                    coding_agent_session=self_attr[
                        "coding_agent_session"
                    ],
                    repo=self_attr["repo"],
                    task_id=self_attr["task_id"],
                ),
            }
        else:
            attr_map = {
                session: SwarmAttribution(is_swarm_worker=False),
            }

        if include_swarm_workers:
            workers = await db.get_swarm_sessions_for_agent(
                session,
                swarm_id=swarm_id,
                worker_name=worker_name,
                worker_role=worker_role,
            )
            for w in workers:
                worker_session = w["session"]
                if worker_session not in attr_map:
                    sessions.append(worker_session)
                    attr_map[worker_session] = SwarmAttribution(
                        is_swarm_worker=True,
                        swarm_id=w["swarm_id"],
                        swarm_worker_name=w["worker_name"],
                        swarm_worker_role=w["worker_role"],
                        coding_agent_session=w[
                            "coding_agent_session"
                        ],
                        repo=w["repo"],
                        task_id=w["task_id"],
                    )

        return sessions, attr_map

    async def get_overview(
        self,
        db: BackboneDB,
        session: str,
        *,
        since: float | None = None,
        until: float | None = None,
        include_swarm_workers: bool = True,
        swarm_id: str | None = None,
        worker_name: str | None = None,
        worker_role: str | None = None,
    ) -> dict:
        """Build overview analytics for a session (+ swarm workers).

        Returns dict with scope, rows_scanned, deduped_rows, totals,
        breakdown_by_session.
        """
        sessions, attr_map = await self.resolve_sessions(
            db,
            session,
            include_swarm_workers=include_swarm_workers,
            swarm_id=swarm_id,
            worker_name=worker_name,
            worker_role=worker_role,
        )

        rows = await db.query_analytics_rows(
            sessions=sessions, since=since, until=until,
        )

        deduped, original_count = deduplicate_rows(rows)

        # Per-session accumulators
        breakdowns: dict[str, SessionBreakdown] = {}
        for s in sessions:
            breakdowns[s] = SessionBreakdown(
                session=s,
                swarm=attr_map.get(s, SwarmAttribution()),
            )

        totals = OverviewTotals()
        any_cost = False
        all_have_cost = True  # track if every token.usage has cost

        # Timestamp tracking for observed window
        min_ts: float | None = None
        max_ts: float | None = None

        # Duration accumulators
        session_starts: dict[str, float] = {}
        task_starts: dict[str, float] = {}
        tool_starts: dict[tuple[str, str | None], float] = {}  # (session, trace_id)

        for row in deduped:
            sess = row["session"]
            event = row["event"]
            bd = breakdowns.get(sess)
            if bd is None:
                continue
            bd.event_count += 1

            try:
                ts_val = float(row["ts"])
            except (ValueError, TypeError):
                ts_val = None

            if ts_val is not None:
                if min_ts is None or ts_val < min_ts:
                    min_ts = ts_val
                if max_ts is None or ts_val > max_ts:
                    max_ts = ts_val

            data = _safe_json(row.get("data"))

            # Token events
            if event in self._TOKEN_EVENTS:
                tokens = _extract_tokens(data)
                for bucket, count in tokens.items():
                    bd.tokens[bucket] = bd.tokens.get(bucket, 0) + count
                    totals.tokens[bucket] = (
                        totals.tokens.get(bucket, 0) + count
                    )

            # Cost — check on any cost-bearing event (token.usage,
            # message.assistant, etc.)
            if event in self._COST_BEARING_EVENTS:
                cost = _extract_cost(data)
                if cost is not None:
                    bd.cost_usd += cost
                    bd.has_cost = True
                    any_cost = True
                    totals.captured_cost_usd += cost
                elif event in self._TOKEN_EVENTS:
                    # Only token.usage lacking cost affects the flag
                    all_have_cost = False

            # Error events
            if event in self._ERROR_EVENTS:
                bd.error_count += 1
                totals.errors[event] = totals.errors.get(event, 0) + 1

            # Tool events
            elif event in self._TOOL_EVENTS:
                if event == "tool.started":
                    bd.tool_count += 1
                    totals.tool_count += 1
                    trace = row.get("trace_id")
                    if ts_val is not None:
                        tool_starts[(sess, trace)] = ts_val
                elif event == "tool.finished":
                    trace = row.get("trace_id")
                    start = tool_starts.pop((sess, trace), None)
                    dur = _extract_duration(data)
                    if dur is not None:
                        totals.tool_seconds += dur
                    elif start is not None and ts_val is not None:
                        totals.tool_seconds += ts_val - start

            # Session events
            elif event == "session.started":
                if ts_val is not None:
                    session_starts[sess] = ts_val
            elif event == "session.stopped":
                start = session_starts.pop(sess, None)
                if start is not None and ts_val is not None:
                    totals.session_seconds += ts_val - start

            # Task events
            elif event == "task.started":
                if ts_val is not None:
                    task_starts[sess] = ts_val
            elif event == "task.completed":
                start = task_starts.pop(sess, None)
                if start is not None and ts_val is not None:
                    totals.task_seconds += ts_val - start

        # Observed window
        if min_ts is not None and max_ts is not None:
            totals.observed_window_seconds = max_ts - min_ts

        # Cost status
        if any_cost:
            totals.cost_status = "partial" if not all_have_cost else "partial"
        else:
            totals.cost_status = "unavailable"

        # Build response
        breakdown_list = []
        for s in sessions:
            bd = breakdowns[s]
            entry: dict = {
                "session": bd.session,
                "event_count": bd.event_count,
                "tokens": {b: bd.tokens.get(b, 0) for b in _TOKEN_BUCKETS},
                "cost_usd": round(bd.cost_usd, 6),
                "error_count": bd.error_count,
                "tool_count": bd.tool_count,
            }
            sa = bd.swarm
            if sa.is_swarm_worker:
                entry["is_swarm_worker"] = True
                entry["swarm_id"] = sa.swarm_id
                entry["swarm_worker_name"] = sa.swarm_worker_name
                entry["swarm_worker_role"] = sa.swarm_worker_role
                entry["coding_agent_session"] = sa.coding_agent_session
                entry["repo"] = sa.repo
                entry["task_id"] = sa.task_id
            else:
                entry["is_swarm_worker"] = False
            breakdown_list.append(entry)

        return {
            "scope": {
                "session": session,
                "since": since,
                "until": until,
                "include_swarm_workers": include_swarm_workers,
                "sessions_queried": sessions,
            },
            "rows_scanned": original_count,
            "deduped_rows": len(deduped),
            "totals": {
                "tokens": {b: totals.tokens.get(b, 0) for b in _TOKEN_BUCKETS},
                "captured_cost_usd": round(totals.captured_cost_usd, 6),
                "cost_status": totals.cost_status,
                "errors": totals.errors,
                "tool_count": totals.tool_count,
                "duration": {
                    "observed_window_seconds": round(totals.observed_window_seconds, 2),
                    "session_seconds": round(totals.session_seconds, 2),
                    "task_seconds": round(totals.task_seconds, 2),
                    "tool_seconds": round(totals.tool_seconds, 2),
                    "best_effort": True,
                },
            },
            "breakdown_by_session": breakdown_list,
        }

    async def get_events(
        self,
        db: BackboneDB,
        session: str,
        *,
        since: float | None = None,
        until: float | None = None,
        include_swarm_workers: bool = True,
        swarm_id: str | None = None,
        worker_name: str | None = None,
        worker_role: str | None = None,
        event: str | None = None,
        runtime: str | None = None,
        model: str | None = None,
        source_kind: str | None = None,
        source_ref: str | None = None,
        trace_id: str | None = None,
        parent_trace_id: str | None = None,
        cursor_ts: str | None = None,
        cursor_id: int | None = None,
        limit: int = 100,
    ) -> dict:
        """Paginated event feed with stable-column filters."""
        sessions, attr_map = await self.resolve_sessions(
            db,
            session,
            include_swarm_workers=include_swarm_workers,
            swarm_id=swarm_id,
            worker_name=worker_name,
            worker_role=worker_role,
        )

        events_filter = [event] if event else None

        # Over-fetch to account for dedup removal, then trim.
        # Fetch 2x+1 so after dedup we likely still have enough.
        fetch_limit = (limit * 2) + 1
        rows = await db.query_analytics_rows(
            sessions=sessions,
            since=since,
            until=until,
            events=events_filter,
            runtime=runtime,
            model=model,
            source_kind=source_kind,
            source_ref=source_ref,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            cursor_ts=cursor_ts,
            cursor_id=cursor_id,
            limit=fetch_limit,
        )

        deduped, _ = deduplicate_rows(rows)

        has_more = len(deduped) > limit
        if has_more:
            deduped = deduped[:limit]

        next_cursor = None
        if has_more and deduped:
            last = deduped[-1]
            next_cursor = {"ts": str(last["ts"]), "id": last["id"]}

        rows = deduped

        # Enrich rows with swarm attribution
        enriched = []
        for row in rows:
            entry = {
                "id": row["id"],
                "session": row["session"],
                "event": row["event"],
                "entity": row.get("entity"),
                "runtime": row.get("runtime"),
                "model": row.get("model"),
                "source_kind": row.get("source_kind"),
                "source_ref": row.get("source_ref"),
                "source_event_id": row.get("source_event_id"),
                "trace_id": row.get("trace_id"),
                "parent_trace_id": row.get("parent_trace_id"),
                "ts": row["ts"],
                "data": _safe_json(row.get("data")),
            }
            sa = attr_map.get(row["session"], SwarmAttribution())
            _apply_swarm_attribution(entry, sa)
            enriched.append(entry)

        return {
            "events": enriched,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    async def get_tools(
        self,
        db: BackboneDB,
        session: str,
        *,
        since: float | None = None,
        until: float | None = None,
        include_swarm_workers: bool = True,
        swarm_id: str | None = None,
        worker_name: str | None = None,
        worker_role: str | None = None,
    ) -> dict:
        """Grouped tool statistics."""
        sessions, _ = await self.resolve_sessions(
            db,
            session,
            include_swarm_workers=include_swarm_workers,
            swarm_id=swarm_id,
            worker_name=worker_name,
            worker_role=worker_role,
        )

        rows = await db.query_analytics_rows(
            sessions=sessions,
            since=since,
            until=until,
            events=["tool.started", "tool.finished", "tool.error"],
        )

        deduped, _ = deduplicate_rows(rows)

        # Accumulate per-tool stats
        stats: dict[str, ToolStat] = {}
        # (session, trace) -> (tool_name, start_ts)
        tool_starts: dict[tuple[str, str | None], tuple[str, float]] = {}

        for row in deduped:
            event = row["event"]
            data = _safe_json(row.get("data"))
            tool_name = _extract_tool_name(data)

            if event == "tool.started":
                if tool_name not in stats:
                    stats[tool_name] = ToolStat(tool_name=tool_name)
                stats[tool_name].call_count += 1

                try:
                    ts_val = float(row["ts"])
                    tool_starts[(row["session"], row.get("trace_id"))] = (tool_name, ts_val)
                except (ValueError, TypeError):
                    pass

            elif event == "tool.finished":
                trace_key = (row["session"], row.get("trace_id"))
                start_info = tool_starts.pop(trace_key, None)
                name = _extract_tool_name(data) if start_info is None else start_info[0]
                if name not in stats:
                    stats[name] = ToolStat(tool_name=name)

                dur = _extract_duration(data)
                if dur is not None:
                    stats[name].total_duration_seconds += dur
                elif start_info is not None:
                    try:
                        ts_val = float(row["ts"])
                        stats[name].total_duration_seconds += ts_val - start_info[1]
                    except (ValueError, TypeError):
                        pass

            elif event == "tool.error":
                if tool_name not in stats:
                    stats[tool_name] = ToolStat(tool_name=tool_name)
                stats[tool_name].error_count += 1

        # Sort by call count descending
        sorted_tools = sorted(stats.values(), key=lambda t: t.call_count, reverse=True)

        return {
            "tools": [
                {
                    "tool_name": t.tool_name,
                    "call_count": t.call_count,
                    "error_count": t.error_count,
                    "total_duration_seconds": round(t.total_duration_seconds, 3),
                }
                for t in sorted_tools
            ],
            "total_tools": len(sorted_tools),
        }

    async def get_errors(
        self,
        db: BackboneDB,
        session: str,
        *,
        since: float | None = None,
        until: float | None = None,
        include_swarm_workers: bool = True,
        swarm_id: str | None = None,
        worker_name: str | None = None,
        worker_role: str | None = None,
    ) -> dict:
        """Error summary + error feed."""
        sessions, attr_map = await self.resolve_sessions(
            db,
            session,
            include_swarm_workers=include_swarm_workers,
            swarm_id=swarm_id,
            worker_name=worker_name,
            worker_role=worker_role,
        )

        rows = await db.query_analytics_rows(
            sessions=sessions,
            since=since,
            until=until,
            events=list(self._ERROR_EVENTS),
        )

        deduped, _ = deduplicate_rows(rows)

        summary: dict[str, int] = {}
        feed: list[dict] = []

        for row in deduped:
            event = row["event"]
            summary[event] = summary.get(event, 0) + 1

            data = _safe_json(row.get("data"))
            error_info = _extract_error_info(data)
            sa = attr_map.get(row["session"], SwarmAttribution())

            entry: dict = {
                "session": row["session"],
                "event": event,
                "ts": row["ts"],
                "error_type": error_info["error_type"],
                "message": error_info["message"],
                "tool_name": error_info.get("tool_name"),
            }
            _apply_swarm_attribution(entry, sa)
            feed.append(entry)

        return {
            "summary": summary,
            "total_errors": sum(summary.values()),
            "errors": feed,
        }
