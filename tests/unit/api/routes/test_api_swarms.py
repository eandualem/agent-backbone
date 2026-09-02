"""Tests for the swarm routes at api/routes/swarms.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from agent_backbone.api.deps import get_db
from agent_backbone.services.swarm import SwarmError


class TestDisband:
    async def test_member_that_will_not_stop_is_a_409_not_a_crash(
        self, api_client, auth_headers, api_app
    ):
        fake_db = MagicMock()
        fake_db.swarms.get = AsyncMock(return_value={"name": "research", "status": "active"})
        api_app.dependency_overrides[get_db] = lambda: fake_db
        try:
            with patch(
                "agent_backbone.api.routes.swarms.teardown_swarm",
                new_callable=AsyncMock,
                side_effect=SwarmError("could not stop swarm member session(s): research-scout-1"),
            ):
                resp = await api_client.delete("/api/swarms/research", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_db, None)

        assert resp.status_code == 409
        assert "research-scout-1" in resp.json()["detail"]
