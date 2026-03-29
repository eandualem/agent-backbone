"""Tests for track and run CRUD endpoints."""

from __future__ import annotations

_SAMPLE_TRACK = {
    "id": "bug-fix",
    "name": "Bug Fix Workflow",
    "description": "Standard bug fix process",
    "definition": {
        "states": ["created", "investigating", "fixing", "reviewing", "done"],
        "initial": "created",
        "transitions": [
            {"from": "created", "to": "investigating", "event": "start"},
            {"from": "investigating", "to": "fixing", "event": "found_cause"},
        ],
    },
}


class TestListTracks:
    async def test_empty_list(self, api_client, auth_headers, api_app):
        resp = await api_client.get("/api/tracks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_returns_created_tracks(self, api_client, auth_headers, api_app):
        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.get("/api/tracks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "bug-fix"
        assert data["items"][0]["name"] == "Bug Fix Workflow"


class TestCreateTrack:
    async def test_creates_track(self, api_client, auth_headers, api_app):
        resp = await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "bug-fix"
        assert data["name"] == "Bug Fix Workflow"
        assert data["definition"]["initial"] == "created"
        assert "created_at" in data
        assert "updated_at" in data

    async def test_duplicate_returns_409(self, api_client, auth_headers, api_app):
        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        assert resp.status_code == 409


class TestGetTrack:
    async def test_returns_track(self, api_client, auth_headers, api_app):
        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.get("/api/tracks/bug-fix", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == "bug-fix"

    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.get("/api/tracks/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


class TestUpdateTrack:
    async def test_updates_name(self, api_client, auth_headers, api_app):
        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.put(
            "/api/tracks/bug-fix",
            headers=auth_headers,
            json={"name": "Updated Workflow"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Workflow"

    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.put(
            "/api/tracks/nonexistent",
            headers=auth_headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404


class TestDeleteTrack:
    async def test_deletes_track(self, api_client, auth_headers, api_app):
        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.delete("/api/tracks/bug-fix", headers=auth_headers)
        assert resp.status_code == 204
        # Verify deleted
        resp = await api_client.get("/api/tracks/bug-fix", headers=auth_headers)
        assert resp.status_code == 404

    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.delete("/api/tracks/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


class TestCreateRun:
    async def test_creates_instance(self, api_client, auth_headers, api_app):
        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={
                "id": "inst-1",
                "context": {"issue_id": 42, "repo": "eandualem/agent-backbone"},
                "current_state": "created",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "inst-1"
        assert data["track_id"] == "bug-fix"
        assert data["current_state"] == "created"
        assert data["context"]["issue_id"] == 42

    async def test_emits_run_created_on_runs_namespace(self, api_client, auth_headers, api_app):
        """RDS-86/88: run:created emitted on /runs namespace after creation."""
        from unittest.mock import AsyncMock

        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        mock_sio = AsyncMock()
        api_app.state.sio = mock_sio

        from agent_backbone.services import _locator
        old_sio = _locator._sio
        _locator._sio = mock_sio
        try:
            resp = await api_client.post(
                "/api/tracks/bug-fix/runs",
                headers=auth_headers,
                json={"id": "run-emit-1", "context": {}, "current_state": "created"},
            )
        finally:
            _locator._sio = old_sio

        assert resp.status_code == 201
        # Find the run:created emit call
        found = False
        for call in mock_sio.emit.await_args_list:
            if call.args[0] == "run:created" and call.kwargs.get("namespace") == "/runs":
                found = True
                payload = call.args[1]
                assert payload["id"] == "run-emit-1"
                assert payload["track_id"] == "bug-fix"
                break
        assert found, "run:created was not emitted on /runs namespace"

    async def test_defaults_status_to_active(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        resp = await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "inst-s", "context": {}, "current_state": "created"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "active"

    async def test_accepts_custom_status_on_create(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        resp = await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={
                "id": "inst-p",
                "context": {},
                "current_state": "created",
                "status": "paused",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "paused"

    async def test_track_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.post(
            "/api/tracks/nonexistent/runs",
            headers=auth_headers,
            json={"id": "inst-1", "context": {}, "current_state": "start"},
        )
        assert resp.status_code == 404


class TestLastProjectedState:
    async def test_defaults_to_null_on_create(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        resp = await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "lps-1", "context": {}, "current_state": "created"},
        )
        assert resp.status_code == 201
        assert resp.json()["last_projected_state"] is None

    async def test_persists_on_put(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "lps-2", "context": {}, "current_state": "created"},
        )
        resp = await api_client.put(
            "/api/tracks/runs/lps-2",
            headers=auth_headers,
            json={"last_projected_state": "investigating"},
        )
        assert resp.status_code == 200
        assert resp.json()["last_projected_state"] == "investigating"

    async def test_survives_get_roundtrip(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "lps-3", "context": {}, "current_state": "created"},
        )
        await api_client.put(
            "/api/tracks/runs/lps-3",
            headers=auth_headers,
            json={"last_projected_state": "fixing"},
        )
        resp = await api_client.get(
            "/api/runs", headers=auth_headers
        )
        assert resp.status_code == 200
        inst = next(i for i in resp.json()["items"] if i["id"] == "lps-3")
        assert inst["last_projected_state"] == "fixing"

    async def test_accepted_on_create(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        resp = await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={
                "id": "lps-4",
                "context": {},
                "current_state": "created",
                "last_projected_state": "created",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["last_projected_state"] == "created"


class TestUpdateRunStatus:
    async def test_persists_status_on_put(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "inst-u", "context": {}, "current_state": "created"},
        )
        resp = await api_client.put(
            "/api/tracks/runs/inst-u",
            headers=auth_headers,
            json={"current_state": "done", "status": "completed"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        assert resp.json()["current_state"] == "done"

    async def test_status_survives_get_roundtrip(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "inst-r", "context": {}, "current_state": "created"},
        )
        await api_client.put(
            "/api/tracks/runs/inst-r",
            headers=auth_headers,
            json={"status": "paused"},
        )
        # Verify via list-all endpoint
        resp = await api_client.get(
            "/api/runs", headers=auth_headers
        )
        assert resp.status_code == 200
        inst = next(i for i in resp.json()["items"] if i["id"] == "inst-r")
        assert inst["status"] == "paused"


class TestListRuns:
    async def test_lists_instances(self, api_client, auth_headers, api_app):
        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "inst-1", "context": {}, "current_state": "created"},
        )
        resp = await api_client.get(
            "/api/tracks/bug-fix/runs", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "inst-1"


class TestUpdateRun:
    async def test_updates_state(self, api_client, auth_headers, api_app):
        await api_client.post("/api/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "inst-1", "context": {}, "current_state": "created"},
        )
        resp = await api_client.put(
            "/api/tracks/runs/inst-1",
            headers=auth_headers,
            json={"current_state": "investigating"},
        )
        assert resp.status_code == 200
        assert resp.json()["current_state"] == "investigating"

    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.put(
            "/api/tracks/runs/nonexistent",
            headers=auth_headers,
            json={"current_state": "done"},
        )
        assert resp.status_code == 404


_SECOND_TRACK = {
    "id": "code-review",
    "name": "Code Review Workflow",
    "description": "Standard code review process",
    "definition": {
        "states": ["pending", "reviewing", "approved"],
        "initial": "pending",
        "transitions": [],
    },
}


class TestListAllRuns:
    async def test_empty_list(self, api_client, auth_headers, api_app):
        resp = await api_client.get(
            "/api/runs", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_returns_instances_across_tracks(
        self, api_client, auth_headers, api_app
    ):
        # Create two tracks with instances
        await api_client.post(
            "/api/tracks",
            headers=auth_headers,
            json=_SAMPLE_TRACK,
        )
        await api_client.post(
            "/api/tracks",
            headers=auth_headers,
            json=_SECOND_TRACK,
        )
        await api_client.post(
            "/api/tracks/bug-fix/runs",
            headers=auth_headers,
            json={"id": "bf-1", "context": {}, "current_state": "created"},
        )
        await api_client.post(
            "/api/tracks/code-review/runs",
            headers=auth_headers,
            json={"id": "cr-1", "context": {}, "current_state": "pending"},
        )

        resp = await api_client.get(
            "/api/runs", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        ids = {item["id"] for item in data["items"]}
        assert ids == {"bf-1", "cr-1"}
        track_ids = {item["track_id"] for item in data["items"]}
        assert track_ids == {"bug-fix", "code-review"}
