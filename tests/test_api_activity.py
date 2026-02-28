"""Tests for api/routes/activity.py — activity timeline endpoint."""

from __future__ import annotations

import json
from dataclasses import replace

from agent_backbone.config import AgentStateConfig


def _set_state_dir(api_app, path: str):
    """Override agent_state.state_dir in the app config."""
    config = api_app.state.config
    api_app.state.config = replace(
        config,
        agent_state=AgentStateConfig(state_dir=path),
    )


class TestGetActivityTimeline:
    """Tests for GET /api/activity/timeline."""

    async def test_returns_merged_timeline(self, api_client, auth_headers, tmp_path, api_app):
        """Returns events from actions, deliveries, and heartbeats merged by timestamp."""
        # Setup actions file
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        actions_file = state_dir / "github-actions.jsonl"
        actions_file.write_text(
            json.dumps({"ts": 3000.0, "session": "ike", "action": "opened", "issue": 42})
        )
        _set_state_dir(api_app, str(state_dir))

        # Setup deliveries and heartbeats in the app's DB instance
        db = api_app.state.db
        await db.record_delivery(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
        )
        await db.record_heartbeat("feynman", "delivered", "test heartbeat")

        resp = await api_client.get("/api/activity/timeline", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        types = {item["type"] for item in data["items"]}
        assert "action" in types
        assert "delivery" in types
        assert "heartbeat" in types

    async def test_sorted_by_timestamp_descending(
        self, api_client, auth_headers, tmp_path, api_app
    ):
        """Events are sorted by timestamp descending."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        actions_file = state_dir / "github-actions.jsonl"
        lines = [
            json.dumps({"ts": 1000.0, "session": "ike", "action": "opened", "issue": 1}),
            json.dumps({"ts": 5000.0, "session": "leo", "action": "closed", "issue": 2}),
            json.dumps({"ts": 3000.0, "session": "ada", "action": "commented", "issue": 3}),
        ]
        actions_file.write_text("\n".join(lines))
        _set_state_dir(api_app, str(state_dir))

        resp = await api_client.get("/api/activity/timeline", headers=auth_headers)

        assert resp.status_code == 200
        items = resp.json()["items"]
        timestamps = [item["ts"] for item in items]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_limit_parameter(self, api_client, auth_headers, tmp_path, api_app):
        """Limit parameter caps the number of returned events."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        actions_file = state_dir / "github-actions.jsonl"
        lines = [
            json.dumps({"ts": float(i), "session": "ike", "action": "opened", "issue": i})
            for i in range(10)
        ]
        actions_file.write_text("\n".join(lines))
        _set_state_dir(api_app, str(state_dir))

        resp = await api_client.get("/api/activity/timeline?limit=3", headers=auth_headers)

        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3

    async def test_offset_parameter(self, api_client, auth_headers, tmp_path, api_app):
        """Offset parameter skips the first N events."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        actions_file = state_dir / "github-actions.jsonl"
        lines = [
            json.dumps({"ts": float(i), "session": "ike", "action": "opened", "issue": i})
            for i in range(10)
        ]
        actions_file.write_text("\n".join(lines))
        _set_state_dir(api_app, str(state_dir))

        resp = await api_client.get("/api/activity/timeline?limit=3&offset=2", headers=auth_headers)

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        # Sorted desc: 9,8,7,6,5... offset=2 skips 9,8 → first item is 7
        assert items[0]["ts"] == 7.0

    async def test_empty_when_no_sources(self, api_client, auth_headers, tmp_path, api_app):
        """Returns empty list when no data sources have entries."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_state_dir(api_app, str(state_dir))

        resp = await api_client.get("/api/activity/timeline", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []
