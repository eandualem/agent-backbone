"""Tests for api/routes/heartbeats.py — heartbeat schedule and history endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from api.deps import get_db
from src.persistence import BackboneDB


@pytest.fixture
def heartbeats_app(api_app):
    """App with get_db overridden to use an in-memory DB seeded with heartbeat records."""

    async def _seed_and_yield():
        async with BackboneDB(":memory:") as db:
            await db.record_heartbeat("ike", "delivered", "Heartbeat check-in")
            await db.record_heartbeat("ike", "delivered", "Second heartbeat")
            await db.record_heartbeat("feynman", "failed", "Session offline")
            yield db

    api_app.dependency_overrides[get_db] = _seed_and_yield
    yield api_app
    api_app.dependency_overrides.clear()


@pytest.fixture
async def client(heartbeats_app):
    """Async test client bound to the heartbeats app."""
    transport = ASGITransport(app=heartbeats_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/heartbeats/schedules
# ---------------------------------------------------------------------------


class TestGetHeartbeatSchedules:
    async def test_returns_schedules(self, client, auth_headers):
        """Returns heartbeat schedules loaded from config path."""
        mock_schedules = {"ike": {"cron": "0 * * * *", "enabled": True}}
        with patch("flows.agent_heartbeat.load_schedules", return_value=mock_schedules):
            resp = await client.get("/api/heartbeats/schedules", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "ike" in data
        assert data["ike"]["cron"] == "0 * * * *"
        assert data["ike"]["enabled"] is True

    async def test_requires_auth(self, client, api_key):
        """Request without auth headers is rejected."""
        resp = await client.get("/api/heartbeats/schedules")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PUT /api/heartbeats/schedules/{agent}
# ---------------------------------------------------------------------------


class TestUpdateHeartbeatSchedule:
    async def test_updates_agent_schedule(self, client, auth_headers):
        """Updates a schedule for a specific agent and saves it."""
        existing = {"ike": {"cron": "0 * * * *", "enabled": True}}
        new_schedule = {"cron": "*/30 * * * *", "enabled": False}

        with (
            patch("flows.agent_heartbeat.load_schedules", return_value=existing),
            patch("flows.agent_heartbeat.save_schedules") as mock_save,
        ):
            resp = await client.put(
                "/api/heartbeats/schedules/ike",
                json=new_schedule,
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["agent"] == "ike"
        # Verify save was called with the merged schedules
        saved_schedules = mock_save.call_args[0][0]
        assert saved_schedules["ike"] == new_schedule

    async def test_adds_new_agent_schedule(self, client, auth_headers):
        """Adding a schedule for a new agent that did not exist before."""
        existing = {"ike": {"cron": "0 * * * *", "enabled": True}}
        new_schedule = {"cron": "0 */2 * * *", "enabled": True}

        with (
            patch("flows.agent_heartbeat.load_schedules", return_value=existing),
            patch("flows.agent_heartbeat.save_schedules") as mock_save,
        ):
            resp = await client.put(
                "/api/heartbeats/schedules/leo",
                json=new_schedule,
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["agent"] == "leo"
        saved_schedules = mock_save.call_args[0][0]
        assert "leo" in saved_schedules
        assert "ike" in saved_schedules  # existing entry preserved


# ---------------------------------------------------------------------------
# GET /api/heartbeats/history
# ---------------------------------------------------------------------------


class TestGetHeartbeatHistory:
    async def test_returns_all_history(self, client, auth_headers):
        """Returns all heartbeat records when no filters applied."""
        resp = await client.get("/api/heartbeats/history", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    async def test_filter_by_agent(self, client, auth_headers):
        """Filters heartbeat records by agent name."""
        resp = await client.get("/api/heartbeats/history?agent=ike", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert all(item["agent"] == "ike" for item in data["items"])

    async def test_filter_by_outcome(self, client, auth_headers):
        """Filters heartbeat records by outcome."""
        resp = await client.get("/api/heartbeats/history?outcome=failed", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["agent"] == "feynman"
        assert data["items"][0]["outcome"] == "failed"

    async def test_respects_limit(self, client, auth_headers):
        """Limit parameter caps the number of returned records."""
        resp = await client.get("/api/heartbeats/history?limit=1", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
