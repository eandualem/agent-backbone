"""Tests for api/routes/deliveries.py — delivery history and stats endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agent_backbone.services.persistence import BackboneDB
from api.deps import get_db


@pytest.fixture
async def deliveries_app(api_app):
    """App with get_db overridden to use an in-memory DB seeded with delivery records."""
    db = BackboneDB("sqlite+aiosqlite:///:memory:")
    await db.start()
    # Seed test data: 3 deliveries across 2 issues and 3 outcomes
    await db.record_delivery(42, "ike", "ike", "delivered", "dispatcher")
    await db.record_delivery(42, "leo", "leo", "offline", "dispatcher")
    await db.record_delivery(43, "ike", "ike", "deferred", "monitor")

    api_app.dependency_overrides[get_db] = lambda: db
    yield api_app
    api_app.dependency_overrides.clear()
    await db.stop()


@pytest.fixture
async def client(deliveries_app):
    """Async test client bound to the deliveries app."""
    transport = ASGITransport(app=deliveries_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestListDeliveries:
    """GET /api/deliveries with optional query filters."""

    async def test_list_all_returns_seeded_records(self, client, auth_headers):
        resp = await client.get("/api/deliveries", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3

    async def test_filter_by_issue_number(self, client, auth_headers):
        resp = await client.get("/api/deliveries?issue_number=42", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert all(item["issue_number"] == 42 for item in body["items"])

    async def test_filter_by_outcome(self, client, auth_headers):
        resp = await client.get("/api/deliveries?outcome=delivered", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["outcome"] == "delivered"
        assert body["items"][0]["target_entity"] == "ike"

    async def test_filter_by_target_entity(self, client, auth_headers):
        resp = await client.get("/api/deliveries?target_entity=ike", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert all(item["target_entity"] == "ike" for item in body["items"])

    async def test_requires_auth(self, client, api_key):
        resp = await client.get("/api/deliveries")
        assert resp.status_code == 401


class TestFailedDeliveries:
    """GET /api/deliveries/failed — returns offline/deferred/failed only."""

    async def test_returns_only_failed_outcomes(self, client, auth_headers):
        resp = await client.get("/api/deliveries/failed", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        # Seeded data has 1 offline + 1 deferred = 2 failed
        assert body["total"] == 2
        outcomes = {item["outcome"] for item in body["items"]}
        assert outcomes == {"offline", "deferred"}

    async def test_excludes_delivered(self, client, auth_headers):
        resp = await client.get("/api/deliveries/failed", headers=auth_headers)
        body = resp.json()
        assert all(item["outcome"] != "delivered" for item in body["items"])


class TestDeliveryStats:
    """GET /api/deliveries/stats — aggregated counts by outcome."""

    async def test_stats_aggregation(self, client, auth_headers):
        resp = await client.get("/api/deliveries/stats", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert body["delivered"] == 1
        assert body["offline"] == 1
        assert body["deferred"] == 1
        assert body["failed"] == 0
