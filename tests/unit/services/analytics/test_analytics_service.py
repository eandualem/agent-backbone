"""Tests for the analytics service — Python extractors and aggregation."""

from __future__ import annotations

import json

import pytest

from agent_backbone.services.analytics.interface import (
    AnalyticsService,
    _extract_cost,
    _extract_tokens,
    _extract_tool_name,
    _safe_json,
    deduplicate_rows,
)
from agent_backbone.services.database import BackboneDB

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(offset: float = 0.0) -> str:
    """Return a unix timestamp string with optional offset."""
    return str(1710000000.0 + offset)


def _activity_row(
    session: str = "agent-backbone",
    event: str = "token.usage",
    data: dict | None = None,
    ts_offset: float = 0.0,
    **kwargs,
) -> dict:
    """Build a row dict matching agent_activity columns."""
    row = {
        "session": session,
        "event": event,
        "entity": kwargs.get("entity"),
        "runtime": kwargs.get("runtime", "claude"),
        "source_kind": kwargs.get("source_kind"),
        "source_ref": kwargs.get("source_ref"),
        "source_event_id": kwargs.get("source_event_id"),
        "trace_id": kwargs.get("trace_id"),
        "parent_trace_id": kwargs.get("parent_trace_id"),
        "model": kwargs.get("model"),
        "data": json.dumps(data) if data is not None else None,
        "ts": _ts(ts_offset),
    }
    return row


async def _insert_rows(db: BackboneDB, rows: list[dict]) -> None:
    """Insert test rows into agent_activity."""
    for row in rows:
        await db.record_activity(
            session=row["session"],
            event=row["event"],
            data=row.get("data"),
            ts=row["ts"],
            entity=row.get("entity"),
            runtime=row.get("runtime"),
            source_kind=row.get("source_kind"),
            source_ref=row.get("source_ref"),
            source_event_id=row.get("source_event_id"),
            trace_id=row.get("trace_id"),
            parent_trace_id=row.get("parent_trace_id"),
            model=row.get("model"),
        )


# ---------------------------------------------------------------------------
# Unit tests for extractors
# ---------------------------------------------------------------------------


class TestExtractors:
    def test_safe_json_valid(self):
        assert _safe_json('{"a": 1}') == {"a": 1}

    def test_safe_json_none(self):
        assert _safe_json(None) == {}

    def test_safe_json_invalid(self):
        assert _safe_json("not json") == {}

    def test_safe_json_non_dict(self):
        assert _safe_json("[1,2,3]") == {}

    def test_extract_tokens_claude(self):
        data = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 30}
        tokens = _extract_tokens(data)
        assert tokens == {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 30}

    def test_extract_tokens_gemini(self):
        data = {"prompt_tokens": 200, "completion_tokens": 100}
        tokens = _extract_tokens(data)
        assert tokens == {"input_tokens": 200, "output_tokens": 100}

    def test_extract_tokens_empty(self):
        assert _extract_tokens({}) == {}

    def test_extract_tokens_bad_value(self):
        data = {"input_tokens": "not_a_number"}
        assert _extract_tokens(data) == {}

    def test_extract_cost_present(self):
        assert _extract_cost({"cost_usd": 0.05}) == 0.05

    def test_extract_cost_alternate_key(self):
        assert _extract_cost({"cost": 1.23}) == 1.23

    def test_extract_cost_missing(self):
        assert _extract_cost({}) is None

    def test_extract_tool_name_present(self):
        assert _extract_tool_name({"tool_name": "Read"}) == "Read"

    def test_extract_tool_name_fallback(self):
        assert _extract_tool_name({"name": "Write"}) == "Write"

    def test_extract_tool_name_unknown(self):
        assert _extract_tool_name({}) == "unknown"


class TestDeduplication:
    def test_dedup_removes_duplicates(self):
        base = {
            "session": "s1",
            "event": "token.usage",
            "source_ref": "a.jsonl",
        }
        rows = [
            {**base, "source_event_id": "1"},
            {**base, "source_event_id": "1"},
            {**base, "source_event_id": "2"},
        ]
        deduped, original = deduplicate_rows(rows)
        assert original == 3
        assert len(deduped) == 2

    def test_dedup_keeps_rows_without_source_ids(self):
        rows = [
            {"session": "s1", "event": "token.usage", "source_ref": None, "source_event_id": None},
            {"session": "s1", "event": "token.usage", "source_ref": None, "source_event_id": None},
        ]
        deduped, _ = deduplicate_rows(rows)
        assert len(deduped) == 2  # cannot dedup without source ids

    def test_dedup_empty(self):
        deduped, original = deduplicate_rows([])
        assert original == 0
        assert deduped == []


# ---------------------------------------------------------------------------
# Integration tests against in-memory DB
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    async with BackboneDB.connect() as db:
        yield db


@pytest.fixture
def svc():
    return AnalyticsService()


class TestOverview:
    async def test_empty_session(self, db, svc):
        result = await svc.get_overview(db, "nonexistent", include_swarm_workers=False)
        assert result["rows_scanned"] == 0
        assert result["deduped_rows"] == 0
        assert result["totals"]["tokens"]["input_tokens"] == 0
        assert result["totals"]["cost_status"] == "unavailable"

    async def test_token_aggregation(self, db, svc):
        rows = [
            _activity_row(
                event="token.usage",
                data={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
                ts_offset=0,
            ),
            _activity_row(
                event="token.usage",
                data={"input_tokens": 200, "output_tokens": 75, "cost_usd": 0.02},
                ts_offset=1,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert result["totals"]["tokens"]["input_tokens"] == 300
        assert result["totals"]["tokens"]["output_tokens"] == 125
        assert result["totals"]["captured_cost_usd"] == pytest.approx(0.03)
        assert result["totals"]["cost_status"] == "partial"

    async def test_error_counting(self, db, svc):
        rows = [
            _activity_row(event="tool.error", data={"message": "oops"}, ts_offset=0),
            _activity_row(event="runtime.error", data={"message": "crash"}, ts_offset=1),
            _activity_row(event="tool.error", data={"message": "oops2"}, ts_offset=2),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert result["totals"]["errors"]["tool.error"] == 2
        assert result["totals"]["errors"]["runtime.error"] == 1

    async def test_tool_counting(self, db, svc):
        rows = [
            _activity_row(
                event="tool.started", data={"tool_name": "Read"},
                ts_offset=0, trace_id="t1",
            ),
            _activity_row(
                event="tool.finished",
                data={"tool_name": "Read", "duration_ms": 500},
                ts_offset=1, trace_id="t1",
            ),
            _activity_row(
                event="tool.started", data={"tool_name": "Write"},
                ts_offset=2, trace_id="t2",
            ),
            _activity_row(
                event="tool.finished", data={"tool_name": "Write"},
                ts_offset=3, trace_id="t2",
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert result["totals"]["tool_count"] == 2
        assert result["totals"]["duration"]["tool_seconds"] >= 0.5

    async def test_dedup_in_overview(self, db, svc):
        rows = [
            _activity_row(
                event="token.usage",
                data={"input_tokens": 100},
                source_ref="a.jsonl",
                source_event_id="ev1",
                ts_offset=0,
            ),
            _activity_row(
                event="token.usage",
                data={"input_tokens": 100},
                source_ref="a.jsonl",
                source_event_id="ev1",
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert result["rows_scanned"] == 2
        assert result["deduped_rows"] == 1
        assert result["totals"]["tokens"]["input_tokens"] == 100

    async def test_time_range_filter(self, db, svc):
        rows = [
            _activity_row(event="token.usage", data={"input_tokens": 50}, ts_offset=0),
            _activity_row(event="token.usage", data={"input_tokens": 100}, ts_offset=10),
            _activity_row(event="token.usage", data={"input_tokens": 200}, ts_offset=20),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "agent-backbone",
            since=1710000005.0,
            until=1710000015.0,
            include_swarm_workers=False,
        )
        assert result["totals"]["tokens"]["input_tokens"] == 100

    async def test_breakdown_by_session(self, db, svc):
        rows = [
            _activity_row(event="token.usage", data={"input_tokens": 50}, ts_offset=0),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert len(result["breakdown_by_session"]) == 1
        bd = result["breakdown_by_session"][0]
        assert bd["session"] == "agent-backbone"
        assert bd["tokens"]["input_tokens"] == 50
        assert bd["is_swarm_worker"] is False

    async def test_session_duration(self, db, svc):
        rows = [
            _activity_row(event="session.started", ts_offset=0),
            _activity_row(event="session.stopped", ts_offset=60),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert result["totals"]["duration"]["session_seconds"] == pytest.approx(60.0)

    async def test_observed_window(self, db, svc):
        rows = [
            _activity_row(event="token.usage", data={"input_tokens": 10}, ts_offset=0),
            _activity_row(event="token.usage", data={"input_tokens": 20}, ts_offset=100),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert result["totals"]["duration"]["observed_window_seconds"] == pytest.approx(100.0)

    async def test_cost_unavailable(self, db, svc):
        rows = [
            _activity_row(event="token.usage", data={"input_tokens": 100}, ts_offset=0),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert result["totals"]["cost_status"] == "unavailable"

    async def test_missing_data_does_not_crash(self, db, svc):
        """Legacy rows with no data field should not crash analytics."""
        rows = [
            _activity_row(event="token.usage", data=None, ts_offset=0),
            _activity_row(event="custom.event", data=None, ts_offset=1),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(db, "agent-backbone", include_swarm_workers=False)
        assert result["rows_scanned"] == 2


class TestEvents:
    async def test_basic_event_feed(self, db, svc):
        rows = [
            _activity_row(event="token.usage", data={"input_tokens": 10}, ts_offset=0),
            _activity_row(event="tool.started", data={"tool_name": "Read"}, ts_offset=1),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_events(
            db, "agent-backbone", include_swarm_workers=False, limit=10
        )
        assert len(result["events"]) == 2
        assert result["has_more"] is False
        assert result["next_cursor"] is None

    async def test_cursor_pagination(self, db, svc):
        rows = [
            _activity_row(event="token.usage", data={"input_tokens": i}, ts_offset=i)
            for i in range(5)
        ]
        await _insert_rows(db, rows)

        # First page
        page1 = await svc.get_events(
            db, "agent-backbone", include_swarm_workers=False, limit=2
        )
        assert len(page1["events"]) == 2
        assert page1["has_more"] is True
        cursor = page1["next_cursor"]

        # Second page
        page2 = await svc.get_events(
            db, "agent-backbone",
            include_swarm_workers=False,
            limit=2,
            cursor_ts=cursor["ts"],
            cursor_id=cursor["id"],
        )
        assert len(page2["events"]) == 2
        assert page2["has_more"] is True

        # Verify no overlap
        page1_ids = {e["id"] for e in page1["events"]}
        page2_ids = {e["id"] for e in page2["events"]}
        assert page1_ids.isdisjoint(page2_ids)

    async def test_event_filter(self, db, svc):
        rows = [
            _activity_row(event="token.usage", ts_offset=0),
            _activity_row(event="tool.started", ts_offset=1),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_events(
            db, "agent-backbone",
            include_swarm_workers=False,
            event="token.usage",
            limit=10,
        )
        assert len(result["events"]) == 1
        assert result["events"][0]["event"] == "token.usage"

    async def test_runtime_filter(self, db, svc):
        rows = [
            _activity_row(event="token.usage", runtime="claude", ts_offset=0),
            _activity_row(event="token.usage", runtime="gemini", ts_offset=1),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_events(
            db, "agent-backbone",
            include_swarm_workers=False,
            runtime="claude",
            limit=10,
        )
        assert len(result["events"]) == 1

    async def test_events_parse_data_json(self, db, svc):
        rows = [
            _activity_row(event="token.usage", data={"input_tokens": 42}, ts_offset=0),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_events(
            db, "agent-backbone", include_swarm_workers=False, limit=10
        )
        assert result["events"][0]["data"]["input_tokens"] == 42


class TestTools:
    async def test_tool_stats_grouped(self, db, svc):
        rows = [
            _activity_row(
                event="tool.started", data={"tool_name": "Read"},
                ts_offset=0, trace_id="t1",
            ),
            _activity_row(
                event="tool.finished",
                data={"tool_name": "Read", "duration_ms": 100},
                ts_offset=1, trace_id="t1",
            ),
            _activity_row(
                event="tool.started", data={"tool_name": "Read"},
                ts_offset=2, trace_id="t2",
            ),
            _activity_row(
                event="tool.finished",
                data={"tool_name": "Read", "duration_ms": 200},
                ts_offset=3, trace_id="t2",
            ),
            _activity_row(
                event="tool.started", data={"tool_name": "Write"},
                ts_offset=4, trace_id="t3",
            ),
            _activity_row(
                event="tool.finished",
                data={"tool_name": "Write", "duration_ms": 50},
                ts_offset=5, trace_id="t3",
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_tools(db, "agent-backbone", include_swarm_workers=False)
        assert result["total_tools"] == 2
        # Read should come first (2 calls > 1)
        read_tool = next(t for t in result["tools"] if t["tool_name"] == "Read")
        assert read_tool["call_count"] == 2
        assert read_tool["total_duration_seconds"] == pytest.approx(0.3)

        write_tool = next(t for t in result["tools"] if t["tool_name"] == "Write")
        assert write_tool["call_count"] == 1

    async def test_tool_error_counted(self, db, svc):
        rows = [
            _activity_row(event="tool.error", data={"tool_name": "Bash"}, ts_offset=0),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_tools(db, "agent-backbone", include_swarm_workers=False)
        bash_tool = next(t for t in result["tools"] if t["tool_name"] == "Bash")
        assert bash_tool["error_count"] == 1

    async def test_unknown_tool_fallback(self, db, svc):
        rows = [
            _activity_row(event="tool.started", data={}, ts_offset=0),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_tools(db, "agent-backbone", include_swarm_workers=False)
        assert result["tools"][0]["tool_name"] == "unknown"


class TestErrors:
    async def test_error_summary_and_feed(self, db, svc):
        rows = [
            _activity_row(
                event="tool.error",
                data={"error_type": "permission", "message": "denied", "tool_name": "Bash"},
                ts_offset=0,
            ),
            _activity_row(
                event="runtime.error",
                data={"message": "crash"},
                ts_offset=1,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_errors(db, "agent-backbone", include_swarm_workers=False)
        assert result["total_errors"] == 2
        assert result["summary"]["tool.error"] == 1
        assert result["summary"]["runtime.error"] == 1
        assert len(result["errors"]) == 2
        assert result["errors"][0]["error_type"] == "permission"

    async def test_empty_errors(self, db, svc):
        result = await svc.get_errors(db, "agent-backbone", include_swarm_workers=False)
        assert result["total_errors"] == 0
        assert result["errors"] == []


class TestSwarmAttribution:
    async def test_swarm_workers_included(self, db, svc):
        """Swarm workers should appear in overview when include_swarm_workers=True."""
        # Create a swarm with a worker
        swarm_id = await db.create_swarm(
            repo="agent-backbone",
            task_id="767",
            coding_agent_session="agent-backbone",
            workers=[
                {
                    "name": "coder",
                    "role": "coder",
                    "branch": "swarm/767/coder",
                    "worktree_path": "/tmp/coder",
                    "session": "swarm-767-coder",
                },
            ],
        )

        # Insert activity for both sessions
        rows = [
            _activity_row(
                session="agent-backbone",
                event="token.usage",
                data={"input_tokens": 100},
                ts_offset=0,
            ),
            _activity_row(
                session="swarm-767-coder",
                event="token.usage",
                data={"input_tokens": 200},
                ts_offset=1,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "agent-backbone", include_swarm_workers=True,
        )

        # Should have queried both sessions
        assert "agent-backbone" in result["scope"]["sessions_queried"]
        assert "swarm-767-coder" in result["scope"]["sessions_queried"]

        # Total tokens should include both
        assert result["totals"]["tokens"]["input_tokens"] == 300

        # Breakdown should show swarm attribution
        assert len(result["breakdown_by_session"]) == 2
        worker_bd = next(
            bd for bd in result["breakdown_by_session"]
            if bd["session"] == "swarm-767-coder"
        )
        assert worker_bd["is_swarm_worker"] is True
        assert worker_bd["swarm_id"] == swarm_id
        assert worker_bd["swarm_worker_name"] == "coder"
        assert worker_bd["swarm_worker_role"] == "coder"
        assert worker_bd["coding_agent_session"] == "agent-backbone"

    async def test_swarm_workers_excluded(self, db, svc):
        """include_swarm_workers=False should only query the main session."""
        await db.create_swarm(
            repo="agent-backbone",
            task_id="767",
            coding_agent_session="agent-backbone",
            workers=[
                {
                    "name": "coder",
                    "role": "coder",
                    "branch": "swarm/767/coder",
                    "worktree_path": "/tmp/coder",
                    "session": "swarm-767-coder",
                },
            ],
        )

        rows = [
            _activity_row(
                session="agent-backbone", event="token.usage",
                data={"input_tokens": 100}, ts_offset=0,
            ),
            _activity_row(
                session="swarm-767-coder", event="token.usage",
                data={"input_tokens": 200}, ts_offset=1,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "agent-backbone", include_swarm_workers=False,
        )
        assert result["totals"]["tokens"]["input_tokens"] == 100
        assert len(result["scope"]["sessions_queried"]) == 1

    async def test_swarm_filter_by_role(self, db, svc):
        """Filter by worker_role restricts swarm workers."""
        await db.create_swarm(
            repo="agent-backbone",
            task_id="767",
            coding_agent_session="agent-backbone",
            workers=[
                {
                    "name": "coder",
                    "role": "coder",
                    "branch": "swarm/767/coder",
                    "worktree_path": "/tmp/coder",
                    "session": "swarm-767-coder",
                },
                {
                    "name": "scout-a",
                    "role": "scout",
                    "branch": "swarm/767/scout",
                    "worktree_path": "/tmp/scout",
                    "session": "swarm-767-scout",
                },
            ],
        )

        rows = [
            _activity_row(
                session="swarm-767-coder", event="token.usage",
                data={"input_tokens": 100}, ts_offset=0,
            ),
            _activity_row(
                session="swarm-767-scout", event="token.usage",
                data={"input_tokens": 200}, ts_offset=1,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "agent-backbone",
            include_swarm_workers=True,
            worker_role="coder",
        )
        # Should include agent-backbone + swarm-767-coder, but NOT scout
        assert "swarm-767-coder" in result["scope"]["sessions_queried"]
        assert "swarm-767-scout" not in result["scope"]["sessions_queried"]


class TestCodexNestedTokens:
    """Codex puts token usage under {"total": {...}, "last": {...}}."""

    def test_extract_tokens_from_codex_nested(self):
        data = {
            "total": {
                "input_tokens": 100,
                "output_tokens": 50,
                "reasoning_output_tokens": 10,
            },
            "last": {
                "input_tokens": 100,
                "output_tokens": 50,
            },
        }
        tokens = _extract_tokens(data)
        assert tokens["input_tokens"] == 100
        assert tokens["output_tokens"] == 50

    def test_extract_tokens_codex_empty_total(self):
        """Empty total dict falls through to flat keys."""
        data = {"total": {}, "input_tokens": 200}
        tokens = _extract_tokens(data)
        assert tokens["input_tokens"] == 200

    async def test_overview_codex_tokens(self, db, svc):
        """Codex nested token payloads aggregate correctly."""
        rows = [
            _activity_row(
                event="token.usage",
                runtime="codex",
                data={
                    "total": {
                        "input_tokens": 300,
                        "output_tokens": 150,
                    },
                    "last": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                    },
                },
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "agent-backbone", include_swarm_workers=False,
        )
        assert result["totals"]["tokens"]["input_tokens"] == 300
        assert result["totals"]["tokens"]["output_tokens"] == 150


class TestOpenCodeCostOnMessageAssistant:
    """OpenCode records cost on message.assistant, not token.usage."""

    async def test_cost_from_message_assistant(self, db, svc):
        rows = [
            _activity_row(
                event="message.assistant",
                runtime="opencode",
                data={"cost": 1.25, "agent": "default"},
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "agent-backbone", include_swarm_workers=False,
        )
        assert result["totals"]["captured_cost_usd"] == pytest.approx(1.25)
        assert result["totals"]["cost_status"] == "partial"

    async def test_cost_from_mixed_events(self, db, svc):
        """Cost from both token.usage and message.assistant sums."""
        rows = [
            _activity_row(
                event="token.usage",
                data={"input_tokens": 100, "cost_usd": 0.05},
                ts_offset=0,
            ),
            _activity_row(
                event="message.assistant",
                runtime="opencode",
                data={"cost": 1.25},
                ts_offset=1,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "agent-backbone", include_swarm_workers=False,
        )
        assert result["totals"]["captured_cost_usd"] == pytest.approx(1.30)


class TestSwarmAttributionInEventsAndErrors:
    """Full swarm context must appear on events and errors."""

    async def _setup_swarm(self, db):
        swarm_id = await db.create_swarm(
            repo="agent-backbone",
            task_id="767",
            coding_agent_session="agent-backbone",
            workers=[
                {
                    "name": "coder",
                    "role": "coder",
                    "branch": "swarm/767/coder",
                    "worktree_path": "/tmp/coder",
                    "session": "swarm-767-coder",
                },
            ],
        )
        return swarm_id

    async def test_events_include_parent_context(self, db, svc):
        swarm_id = await self._setup_swarm(db)
        rows = [
            _activity_row(
                session="swarm-767-coder",
                event="token.usage",
                data={"input_tokens": 50},
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_events(
            db, "agent-backbone",
            include_swarm_workers=True, limit=10,
        )
        worker_ev = next(
            e for e in result["events"]
            if e["session"] == "swarm-767-coder"
        )
        assert worker_ev["is_swarm_worker"] is True
        assert worker_ev["swarm_id"] == swarm_id
        assert worker_ev["swarm_worker_name"] == "coder"
        assert worker_ev["swarm_worker_role"] == "coder"
        assert worker_ev["coding_agent_session"] == "agent-backbone"
        assert worker_ev["repo"] == "agent-backbone"
        assert worker_ev["task_id"] == "767"

    async def test_errors_include_parent_context(self, db, svc):
        swarm_id = await self._setup_swarm(db)
        rows = [
            _activity_row(
                session="swarm-767-coder",
                event="tool.error",
                data={"message": "fail"},
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_errors(
            db, "agent-backbone",
            include_swarm_workers=True,
        )
        worker_err = result["errors"][0]
        assert worker_err["is_swarm_worker"] is True
        assert worker_err["swarm_id"] == swarm_id
        assert worker_err["coding_agent_session"] == "agent-backbone"
        assert worker_err["repo"] == "agent-backbone"
        assert worker_err["task_id"] == "767"


class TestEventsDedup:
    """Events endpoint must deduplicate replayed rows."""

    async def test_events_dedup_replayed_rows(self, db, svc):
        """Duplicate source events should yield one row in /events."""
        rows = [
            _activity_row(
                event="token.usage",
                data={"input_tokens": 50},
                source_ref="a.jsonl",
                source_event_id="ev1",
                ts_offset=0,
            ),
            _activity_row(
                event="token.usage",
                data={"input_tokens": 50},
                source_ref="a.jsonl",
                source_event_id="ev1",
                ts_offset=0,
            ),
            _activity_row(
                event="token.usage",
                data={"input_tokens": 75},
                source_ref="a.jsonl",
                source_event_id="ev2",
                ts_offset=1,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_events(
            db, "agent-backbone",
            include_swarm_workers=False, limit=10,
        )
        # Should have deduped ev1 → 2 unique events
        assert len(result["events"]) == 2

    async def test_events_dedup_preserves_pagination(self, db, svc):
        """Cursor pagination still works after dedup."""
        for i in range(6):
            rows = [
                _activity_row(
                    event="token.usage",
                    data={"i": i},
                    source_ref="b.jsonl",
                    source_event_id=f"ev{i}",
                    ts_offset=i,
                ),
            ]
            await _insert_rows(db, rows)

        page1 = await svc.get_events(
            db, "agent-backbone",
            include_swarm_workers=False, limit=3,
        )
        assert len(page1["events"]) == 3
        assert page1["has_more"] is True
        cursor = page1["next_cursor"]

        page2 = await svc.get_events(
            db, "agent-backbone",
            include_swarm_workers=False, limit=3,
            cursor_ts=cursor["ts"],
            cursor_id=cursor["id"],
        )
        assert len(page2["events"]) == 3

        ids1 = {e["id"] for e in page1["events"]}
        ids2 = {e["id"] for e in page2["events"]}
        assert ids1.isdisjoint(ids2)


class TestDirectWorkerDrilldown:
    """Querying a worker session directly must resolve swarm context."""

    async def _setup_swarm(self, db):
        return await db.create_swarm(
            repo="agent-backbone",
            task_id="767",
            coding_agent_session="agent-backbone",
            workers=[
                {
                    "name": "coder",
                    "role": "coder",
                    "branch": "swarm/767/coder",
                    "worktree_path": "/tmp/coder",
                    "session": "swarm-767-coder",
                },
            ],
        )

    async def test_direct_worker_errors_has_attribution(
        self, db, svc,
    ):
        swarm_id = await self._setup_swarm(db)
        rows = [
            _activity_row(
                session="swarm-767-coder",
                event="tool.error",
                data={"message": "fail"},
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_errors(
            db, "swarm-767-coder",
            include_swarm_workers=True,
        )
        assert result["total_errors"] == 1
        err = result["errors"][0]
        assert err["is_swarm_worker"] is True
        assert err["swarm_id"] == swarm_id
        assert err["swarm_worker_name"] == "coder"
        assert err["swarm_worker_role"] == "coder"
        assert err["coding_agent_session"] == "agent-backbone"
        assert err["repo"] == "agent-backbone"
        assert err["task_id"] == "767"

    async def test_direct_worker_events_has_attribution(
        self, db, svc,
    ):
        swarm_id = await self._setup_swarm(db)
        rows = [
            _activity_row(
                session="swarm-767-coder",
                event="token.usage",
                data={"input_tokens": 50},
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_events(
            db, "swarm-767-coder",
            include_swarm_workers=True, limit=10,
        )
        ev = result["events"][0]
        assert ev["is_swarm_worker"] is True
        assert ev["swarm_id"] == swarm_id
        assert ev["coding_agent_session"] == "agent-backbone"
        assert ev["repo"] == "agent-backbone"
        assert ev["task_id"] == "767"

    async def test_direct_worker_overview_has_attribution(
        self, db, svc,
    ):
        swarm_id = await self._setup_swarm(db)
        rows = [
            _activity_row(
                session="swarm-767-coder",
                event="token.usage",
                data={"input_tokens": 50},
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "swarm-767-coder",
            include_swarm_workers=True,
        )
        bd = result["breakdown_by_session"][0]
        assert bd["is_swarm_worker"] is True
        assert bd["swarm_id"] == swarm_id
        assert bd["coding_agent_session"] == "agent-backbone"

    async def test_non_worker_session_unaffected(self, db, svc):
        """A non-worker session should still show is_swarm_worker=False."""
        rows = [
            _activity_row(
                event="token.usage",
                data={"input_tokens": 50},
                ts_offset=0,
            ),
        ]
        await _insert_rows(db, rows)

        result = await svc.get_overview(
            db, "agent-backbone",
            include_swarm_workers=False,
        )
        bd = result["breakdown_by_session"][0]
        assert bd["is_swarm_worker"] is False
