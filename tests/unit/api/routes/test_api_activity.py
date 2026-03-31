"""Tests for api/routes/activity.py — activity timeline endpoint."""

from __future__ import annotations


class TestGetActivityAlias:
    """Tests for GET /api/activity (alias for /activity/timeline)."""

    async def test_activity_alias_returns_200(self, api_client, auth_headers, api_app):
        """GET /api/activity returns data from DB sources."""
        db = api_app.state.db
        await db.record_delivery(
            issue_number=1,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
        )

        resp = await api_client.get("/api/activity", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    async def test_activity_alias_matches_timeline(self, api_client, auth_headers):
        """Both /api/activity and /api/activity/timeline return identical responses."""
        resp_alias = await api_client.get("/api/activity", headers=auth_headers)
        resp_timeline = await api_client.get("/api/activity/timeline", headers=auth_headers)

        assert resp_alias.status_code == 200
        assert resp_timeline.status_code == 200
        assert resp_alias.json() == resp_timeline.json()


class TestGetActivityTimeline:
    """Tests for GET /api/activity/timeline."""

    async def test_returns_merged_timeline(self, api_client, auth_headers, api_app):
        """Returns events from deliveries, heartbeats, and telemetry."""
        db = api_app.state.db
        await db.record_delivery(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
        )
        await db.record_heartbeat("feynman", "delivered", "test heartbeat")
        await db.record_activity(
            "ike",
            "task.started",
            None,
            "4000.0",
            entity="ike",
            runtime="claude",
        )

        resp = await api_client.get("/api/activity/timeline", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        types = {item["type"] for item in data["items"]}
        assert "delivery" in types
        assert "heartbeat" in types
        assert "telemetry" in types

    async def test_projects_only_frontend_relevant_telemetry(
        self, api_client, auth_headers, api_app
    ):
        """Timeline projects selected telemetry events and omits low-level tool chatter."""
        db = api_app.state.db
        await db.record_activity(
            "ike",
            "tool.started",
            None,
            "1000.0",
            entity="ike",
            runtime="claude",
        )
        await db.record_activity(
            "ike",
            "task.completed",
            None,
            "2000.0",
            entity="ike",
            runtime="claude",
        )

        resp = await api_client.get("/api/activity/timeline", headers=auth_headers)

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert any(item["type"] == "telemetry" for item in items)
        assert any(item["summary"] == "claude task completed" for item in items)
        assert all(item["summary"] != "claude tool.started" for item in items)

    async def test_sorted_by_timestamp_descending(self, api_client, auth_headers, api_app):
        """Events are sorted by timestamp descending."""
        db = api_app.state.db
        for i, ts in enumerate([1000.0, 5000.0, 3000.0]):
            await db.record_activity("ike", "task.started", None, str(ts), entity="ike")

        resp = await api_client.get("/api/activity/timeline", headers=auth_headers)

        assert resp.status_code == 200
        items = resp.json()["items"]
        timestamps = [item["ts"] for item in items]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_limit_parameter(self, api_client, auth_headers, api_app):
        """Limit parameter caps the number of returned events."""
        db = api_app.state.db
        for i in range(10):
            await db.record_activity(
                "ike", "task.started", None, str(float(i)), entity="ike"
            )

        resp = await api_client.get("/api/activity/timeline?limit=3", headers=auth_headers)

        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3

    async def test_offset_parameter(self, api_client, auth_headers, api_app):
        """Offset parameter skips the first N events."""
        db = api_app.state.db
        for i in range(10):
            await db.record_activity(
                "ike", "task.started", None, str(float(i)), entity="ike"
            )

        resp = await api_client.get(
            "/api/activity/timeline?limit=3&offset=2", headers=auth_headers
        )

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        # Sorted desc: 9,8,7,6,5... offset=2 skips 9,8 → first item is 7
        assert items[0]["ts"] == 7.0

    async def test_empty_when_no_sources(self, api_client, auth_headers):
        """Returns empty list when no data sources have entries."""
        resp = await api_client.get("/api/activity/timeline", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []
