"""Tests for api/routes/heartbeats.py — heartbeat schedule and history endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.api.deps import get_db, get_monitoring_service
from agent_backbone.services.database import BackboneDB


@pytest.fixture
async def heartbeats_app(api_app):
    """App with get_db overridden to use an in-memory DB seeded with heartbeat records."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    db = BackboneDB(engine)
    await db.start()
    await db.record_heartbeat("ike", "delivered", "Heartbeat check-in")
    await db.record_heartbeat("ike", "delivered", "Second heartbeat")
    await db.record_heartbeat("feynman", "failed", "Session offline")

    api_app.dependency_overrides[get_db] = lambda: db
    yield api_app
    api_app.dependency_overrides.clear()
    db._engine = None
    await engine.dispose()


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
    async def test_returns_schedules(self, client, auth_headers, heartbeats_app):
        """Returns heartbeat schedules loaded from config path."""
        mock_schedules = {"ike": {"cron": "0 * * * *", "enabled": True}}
        mock_mon_svc = MagicMock()
        mock_mon_svc.load_schedules = MagicMock(return_value=mock_schedules)

        heartbeats_app.dependency_overrides[get_monitoring_service] = lambda: mock_mon_svc
        try:
            resp = await client.get("/api/heartbeats/schedules", headers=auth_headers)
        finally:
            heartbeats_app.dependency_overrides.pop(get_monitoring_service, None)

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
    async def test_updates_agent_schedule(self, client, auth_headers, heartbeats_app):
        """Updates a schedule for a specific agent and saves it."""
        existing = {"ike": {"cron": "0 * * * *", "enabled": True}}
        new_schedule = {"cron": "*/30 * * * *", "enabled": False}

        mock_mon_svc = MagicMock()
        mock_mon_svc.load_schedules = MagicMock(return_value=existing)
        mock_mon_svc.save_schedules = MagicMock()

        heartbeats_app.dependency_overrides[get_monitoring_service] = lambda: mock_mon_svc
        with patch(
            "agent_backbone.api.routes.heartbeats.notify_stream",
            new_callable=AsyncMock,
        ) as mock_notify:
            try:
                resp = await client.put(
                    "/api/heartbeats/schedules/ike",
                    json=new_schedule,
                    headers=auth_headers,
                )
            finally:
                heartbeats_app.dependency_overrides.pop(get_monitoring_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["agent"] == "ike"
        # Verify save was called with the merged schedules
        saved_schedules = mock_mon_svc.save_schedules.call_args[0][0]
        assert saved_schedules["ike"] == new_schedule
        mock_notify.assert_awaited_once_with("schedule")

    async def test_adds_new_agent_schedule(self, client, auth_headers, heartbeats_app):
        """Adding a schedule for a new agent that did not exist before."""
        existing = {"ike": {"cron": "0 * * * *", "enabled": True}}
        new_schedule = {"cron": "0 */2 * * *", "enabled": True}

        mock_mon_svc = MagicMock()
        mock_mon_svc.load_schedules = MagicMock(return_value=existing)
        mock_mon_svc.save_schedules = MagicMock()

        heartbeats_app.dependency_overrides[get_monitoring_service] = lambda: mock_mon_svc
        try:
            resp = await client.put(
                "/api/heartbeats/schedules/leo",
                json=new_schedule,
                headers=auth_headers,
            )
        finally:
            heartbeats_app.dependency_overrides.pop(get_monitoring_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["agent"] == "leo"
        saved_schedules = mock_mon_svc.save_schedules.call_args[0][0]
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
