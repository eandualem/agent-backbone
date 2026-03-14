"""Tests for analytics API routes."""

from __future__ import annotations

import json


def _ts(offset: float = 0.0) -> str:
    return str(1710000000.0 + offset)


async def _insert_activity(api_app, session, event, data=None, ts_offset=0.0, **kwargs):
    """Insert an activity row via the db directly."""
    db = api_app.state.db
    await db.record_activity(
        session=session,
        event=event,
        data=json.dumps(data) if data is not None else None,
        ts=_ts(ts_offset),
        runtime=kwargs.get("runtime"),
        source_kind=kwargs.get("source_kind"),
        source_ref=kwargs.get("source_ref"),
        source_event_id=kwargs.get("source_event_id"),
        trace_id=kwargs.get("trace_id"),
        parent_trace_id=kwargs.get("parent_trace_id"),
        model=kwargs.get("model"),
        entity=kwargs.get("entity"),
    )


class TestOverviewEndpoint:
    async def test_overview_empty(self, api_client, auth_headers):
        resp = await api_client.get(
            "/api/analytics/agents/nonexistent/overview",
            headers=auth_headers,
            params={"include_swarm_workers": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rows_scanned"] == 0
        assert data["totals"]["cost_status"] == "unavailable"

    async def test_overview_with_tokens(self, api_client, auth_headers, api_app):
        await _insert_activity(
            api_app, "test-agent", "token.usage",
            data={"input_tokens": 150, "output_tokens": 75, "cost_usd": 0.02},
        )

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/overview",
            headers=auth_headers,
            params={"include_swarm_workers": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totals"]["tokens"]["input_tokens"] == 150
        assert data["totals"]["tokens"]["output_tokens"] == 75
        assert data["totals"]["captured_cost_usd"] > 0

    async def test_overview_with_errors(self, api_client, auth_headers, api_app):
        await _insert_activity(api_app, "test-agent", "tool.error", data={"message": "fail"})
        await _insert_activity(
            api_app, "test-agent", "runtime.error",
            data={"message": "crash"}, ts_offset=1,
        )

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/overview",
            headers=auth_headers,
            params={"include_swarm_workers": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totals"]["errors"]["tool.error"] == 1
        assert data["totals"]["errors"]["runtime.error"] == 1

    async def test_overview_time_filter(self, api_client, auth_headers, api_app):
        await _insert_activity(
            api_app, "test-agent", "token.usage",
            data={"input_tokens": 50}, ts_offset=0,
        )
        await _insert_activity(
            api_app, "test-agent", "token.usage",
            data={"input_tokens": 100}, ts_offset=100,
        )

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/overview",
            headers=auth_headers,
            params={
                "since": 1710000050.0,
                "until": 1710000200.0,
                "include_swarm_workers": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totals"]["tokens"]["input_tokens"] == 100

    async def test_overview_requires_auth(self, api_client, api_key):
        resp = await api_client.get("/api/analytics/agents/test-agent/overview")
        assert resp.status_code == 401


class TestEventsEndpoint:
    async def test_events_basic(self, api_client, auth_headers, api_app):
        await _insert_activity(api_app, "test-agent", "token.usage", ts_offset=0)
        await _insert_activity(api_app, "test-agent", "tool.started", ts_offset=1)

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/events",
            headers=auth_headers,
            params={"include_swarm_workers": False, "limit": 10},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 2
        assert data["has_more"] is False

    async def test_events_pagination(self, api_client, auth_headers, api_app):
        for i in range(5):
            await _insert_activity(
                api_app, "test-agent", "token.usage",
                data={"i": i}, ts_offset=i,
            )

        # First page
        resp = await api_client.get(
            "/api/analytics/agents/test-agent/events",
            headers=auth_headers,
            params={"include_swarm_workers": False, "limit": 2},
        )
        data = resp.json()
        assert len(data["events"]) == 2
        assert data["has_more"] is True
        cursor = data["next_cursor"]

        # Second page
        resp2 = await api_client.get(
            "/api/analytics/agents/test-agent/events",
            headers=auth_headers,
            params={
                "include_swarm_workers": False,
                "limit": 2,
                "cursor_ts": cursor["ts"],
                "cursor_id": cursor["id"],
            },
        )
        data2 = resp2.json()
        assert len(data2["events"]) == 2
        # No overlap
        ids1 = {e["id"] for e in data["events"]}
        ids2 = {e["id"] for e in data2["events"]}
        assert ids1.isdisjoint(ids2)

    async def test_events_event_filter(self, api_client, auth_headers, api_app):
        await _insert_activity(api_app, "test-agent", "token.usage", ts_offset=0)
        await _insert_activity(api_app, "test-agent", "tool.started", ts_offset=1)

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/events",
            headers=auth_headers,
            params={"include_swarm_workers": False, "event": "token.usage"},
        )
        data = resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["event"] == "token.usage"

    async def test_events_runtime_filter(self, api_client, auth_headers, api_app):
        await _insert_activity(
            api_app, "test-agent", "token.usage",
            runtime="claude", ts_offset=0,
        )
        await _insert_activity(
            api_app, "test-agent", "token.usage",
            runtime="gemini", ts_offset=1,
        )

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/events",
            headers=auth_headers,
            params={"include_swarm_workers": False, "runtime": "claude"},
        )
        data = resp.json()
        assert len(data["events"]) == 1


class TestToolsEndpoint:
    async def test_tools_grouped(self, api_client, auth_headers, api_app):
        await _insert_activity(
            api_app, "test-agent", "tool.started",
            data={"tool_name": "Read"}, trace_id="t1", ts_offset=0,
        )
        await _insert_activity(
            api_app, "test-agent", "tool.finished",
            data={"tool_name": "Read", "duration_ms": 100}, trace_id="t1", ts_offset=1,
        )
        await _insert_activity(
            api_app, "test-agent", "tool.started",
            data={"tool_name": "Read"}, trace_id="t2", ts_offset=2,
        )
        await _insert_activity(
            api_app, "test-agent", "tool.finished",
            data={"tool_name": "Read", "duration_ms": 200}, trace_id="t2", ts_offset=3,
        )

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/tools",
            headers=auth_headers,
            params={"include_swarm_workers": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_tools"] == 1
        assert data["tools"][0]["tool_name"] == "Read"
        assert data["tools"][0]["call_count"] == 2

    async def test_tools_empty(self, api_client, auth_headers):
        resp = await api_client.get(
            "/api/analytics/agents/test-agent/tools",
            headers=auth_headers,
            params={"include_swarm_workers": False},
        )
        assert resp.status_code == 200
        assert resp.json()["total_tools"] == 0


class TestErrorsEndpoint:
    async def test_errors_summary_and_feed(self, api_client, auth_headers, api_app):
        await _insert_activity(
            api_app, "test-agent", "tool.error",
            data={"error_type": "permission", "message": "denied", "tool_name": "Bash"},
        )
        await _insert_activity(
            api_app, "test-agent", "runtime.error",
            data={"message": "crash"}, ts_offset=1,
        )

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/errors",
            headers=auth_headers,
            params={"include_swarm_workers": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_errors"] == 2
        assert data["summary"]["tool.error"] == 1
        assert len(data["errors"]) == 2

    async def test_errors_empty(self, api_client, auth_headers):
        resp = await api_client.get(
            "/api/analytics/agents/test-agent/errors",
            headers=auth_headers,
            params={"include_swarm_workers": False},
        )
        assert resp.status_code == 200
        assert resp.json()["total_errors"] == 0


class TestSwarmAttributionInRoutes:
    async def test_overview_includes_swarm_workers(self, api_client, auth_headers, api_app):
        db = api_app.state.db
        await db.create_swarm(
            repo="test-repo",
            task_id="100",
            coding_agent_session="test-agent",
            workers=[
                {
                    "name": "coder",
                    "role": "coder",
                    "branch": "swarm/100/coder",
                    "worktree_path": "/tmp/coder",
                    "session": "swarm-100-coder",
                },
            ],
        )

        await _insert_activity(
            api_app, "test-agent", "token.usage",
            data={"input_tokens": 100}, ts_offset=0,
        )
        await _insert_activity(
            api_app, "swarm-100-coder", "token.usage",
            data={"input_tokens": 200}, ts_offset=1,
        )

        resp = await api_client.get(
            "/api/analytics/agents/test-agent/overview",
            headers=auth_headers,
            params={"include_swarm_workers": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totals"]["tokens"]["input_tokens"] == 300
        assert len(data["breakdown_by_session"]) == 2

        worker_bd = next(
            bd for bd in data["breakdown_by_session"]
            if bd["session"] == "swarm-100-coder"
        )
        assert worker_bd["is_swarm_worker"] is True
        assert worker_bd["swarm_worker_name"] == "coder"
