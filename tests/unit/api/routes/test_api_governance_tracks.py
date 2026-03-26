"""Tests for governance track and instance CRUD endpoints."""

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
        resp = await api_client.get("/api/governance/tracks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_returns_created_tracks(self, api_client, auth_headers, api_app):
        await api_client.post("/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.get("/api/governance/tracks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "bug-fix"
        assert data["items"][0]["name"] == "Bug Fix Workflow"


class TestCreateTrack:
    async def test_creates_track(self, api_client, auth_headers, api_app):
        resp = await api_client.post(
            "/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "bug-fix"
        assert data["name"] == "Bug Fix Workflow"
        assert data["definition"]["initial"] == "created"
        assert "created_at" in data
        assert "updated_at" in data

    async def test_duplicate_returns_409(self, api_client, auth_headers, api_app):
        await api_client.post("/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.post(
            "/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        assert resp.status_code == 409


class TestGetTrack:
    async def test_returns_track(self, api_client, auth_headers, api_app):
        await api_client.post("/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.get("/api/governance/tracks/bug-fix", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == "bug-fix"

    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.get("/api/governance/tracks/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


class TestUpdateTrack:
    async def test_updates_name(self, api_client, auth_headers, api_app):
        await api_client.post("/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.put(
            "/api/governance/tracks/bug-fix",
            headers=auth_headers,
            json={"name": "Updated Workflow"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Workflow"

    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.put(
            "/api/governance/tracks/nonexistent",
            headers=auth_headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404


class TestDeleteTrack:
    async def test_deletes_track(self, api_client, auth_headers, api_app):
        await api_client.post("/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.delete("/api/governance/tracks/bug-fix", headers=auth_headers)
        assert resp.status_code == 204
        # Verify deleted
        resp = await api_client.get("/api/governance/tracks/bug-fix", headers=auth_headers)
        assert resp.status_code == 404

    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.delete("/api/governance/tracks/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


class TestCreateInstance:
    async def test_creates_instance(self, api_client, auth_headers, api_app):
        await api_client.post("/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        resp = await api_client.post(
            "/api/governance/tracks/bug-fix/instances",
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

    async def test_defaults_status_to_active(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        resp = await api_client.post(
            "/api/governance/tracks/bug-fix/instances",
            headers=auth_headers,
            json={"id": "inst-s", "context": {}, "current_state": "created"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "active"

    async def test_accepts_custom_status_on_create(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        resp = await api_client.post(
            "/api/governance/tracks/bug-fix/instances",
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
            "/api/governance/tracks/nonexistent/instances",
            headers=auth_headers,
            json={"id": "inst-1", "context": {}, "current_state": "start"},
        )
        assert resp.status_code == 404


class TestUpdateInstanceStatus:
    async def test_persists_status_on_put(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        await api_client.post(
            "/api/governance/tracks/bug-fix/instances",
            headers=auth_headers,
            json={"id": "inst-u", "context": {}, "current_state": "created"},
        )
        resp = await api_client.put(
            "/api/governance/tracks/instances/inst-u",
            headers=auth_headers,
            json={"current_state": "done", "status": "completed"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        assert resp.json()["current_state"] == "done"

    async def test_status_survives_get_roundtrip(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK
        )
        await api_client.post(
            "/api/governance/tracks/bug-fix/instances",
            headers=auth_headers,
            json={"id": "inst-r", "context": {}, "current_state": "created"},
        )
        await api_client.put(
            "/api/governance/tracks/instances/inst-r",
            headers=auth_headers,
            json={"status": "paused"},
        )
        # Verify via list-all endpoint
        resp = await api_client.get(
            "/api/governance/instances", headers=auth_headers
        )
        assert resp.status_code == 200
        inst = next(i for i in resp.json()["items"] if i["id"] == "inst-r")
        assert inst["status"] == "paused"


class TestListInstances:
    async def test_lists_instances(self, api_client, auth_headers, api_app):
        await api_client.post("/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        await api_client.post(
            "/api/governance/tracks/bug-fix/instances",
            headers=auth_headers,
            json={"id": "inst-1", "context": {}, "current_state": "created"},
        )
        resp = await api_client.get(
            "/api/governance/tracks/bug-fix/instances", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "inst-1"


class TestUpdateInstance:
    async def test_updates_state(self, api_client, auth_headers, api_app):
        await api_client.post("/api/governance/tracks", headers=auth_headers, json=_SAMPLE_TRACK)
        await api_client.post(
            "/api/governance/tracks/bug-fix/instances",
            headers=auth_headers,
            json={"id": "inst-1", "context": {}, "current_state": "created"},
        )
        resp = await api_client.put(
            "/api/governance/tracks/instances/inst-1",
            headers=auth_headers,
            json={"current_state": "investigating"},
        )
        assert resp.status_code == 200
        assert resp.json()["current_state"] == "investigating"

    async def test_not_found_returns_404(self, api_client, auth_headers, api_app):
        resp = await api_client.put(
            "/api/governance/tracks/instances/nonexistent",
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


class TestListAllInstances:
    async def test_empty_list(self, api_client, auth_headers, api_app):
        resp = await api_client.get(
            "/api/governance/instances", headers=auth_headers
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
            "/api/governance/tracks",
            headers=auth_headers,
            json=_SAMPLE_TRACK,
        )
        await api_client.post(
            "/api/governance/tracks",
            headers=auth_headers,
            json=_SECOND_TRACK,
        )
        await api_client.post(
            "/api/governance/tracks/bug-fix/instances",
            headers=auth_headers,
            json={"id": "bf-1", "context": {}, "current_state": "created"},
        )
        await api_client.post(
            "/api/governance/tracks/code-review/instances",
            headers=auth_headers,
            json={"id": "cr-1", "context": {}, "current_state": "pending"},
        )

        resp = await api_client.get(
            "/api/governance/instances", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        ids = {item["id"] for item in data["items"]}
        assert ids == {"bf-1", "cr-1"}
        track_ids = {item["track_id"] for item in data["items"]}
        assert track_ids == {"bug-fix", "code-review"}
