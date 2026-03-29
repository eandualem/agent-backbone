"""Tests for track layout endpoints."""

from __future__ import annotations

_SAMPLE_POSITIONS = {
    "created": {"x": 100, "y": 200},
    "investigating": {"x": 300, "y": 200},
    "fixing": {"x": 500, "y": 200},
}


class TestGetLayout:
    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        """Returns 404 when no layout exists for the track."""
        resp = await api_client.get(
            "/api/tracks/nonexistent/layout", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_returns_saved_layout(self, api_client, auth_headers, api_app):
        """Returns layout after it has been saved."""
        await api_client.post(
            "/api/tracks/bug-fix/layout",
            headers=auth_headers,
            json={"positions": _SAMPLE_POSITIONS},
        )
        resp = await api_client.get(
            "/api/tracks/bug-fix/layout", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["track_id"] == "bug-fix"
        assert data["positions"]["created"]["x"] == 100
        assert "updated_at" in data


class TestUpsertLayout:
    async def test_creates_layout(self, api_client, auth_headers, api_app):
        """Creates a new layout on first POST."""
        resp = await api_client.post(
            "/api/tracks/new-track/layout",
            headers=auth_headers,
            json={"positions": _SAMPLE_POSITIONS},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["track_id"] == "new-track"
        assert data["positions"] == _SAMPLE_POSITIONS

    async def test_updates_existing_layout(self, api_client, auth_headers, api_app):
        """Updates positions on subsequent POST."""
        await api_client.post(
            "/api/tracks/my-track/layout",
            headers=auth_headers,
            json={"positions": {"a": {"x": 0, "y": 0}}},
        )
        updated = {"a": {"x": 50, "y": 75}, "b": {"x": 100, "y": 100}}
        resp = await api_client.post(
            "/api/tracks/my-track/layout",
            headers=auth_headers,
            json={"positions": updated},
        )
        assert resp.status_code == 200
        assert resp.json()["positions"] == updated

        # Verify via GET
        resp = await api_client.get(
            "/api/tracks/my-track/layout", headers=auth_headers
        )
        assert resp.json()["positions"] == updated
