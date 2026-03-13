"""Tests for swarm registry API routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_backbone.api.deps import get_delivery_service


async def _create_swarm(api_client, auth_headers, **overrides) -> str:
    payload = {
        "repo": "agent-backbon",
        "task_id": "739",
        "coding_agent_session": "agent-backbon",
        "workers": [
            {
                "name": "worker-a",
                "branch": "feature/swarm-a",
                "worktree_path": "/tmp/swarm-a",
                "session": "worker-a",
            },
            {
                "name": "worker-b",
                "branch": "feature/swarm-b",
                "worktree_path": "/tmp/swarm-b",
                "session": "worker-b",
            },
        ],
    }
    payload.update(overrides)
    resp = await api_client.post("/api/swarms", headers=auth_headers, json=payload)
    assert resp.status_code == 201
    return resp.json()["swarm_id"]


class TestCreateSwarm:
    async def test_create_swarm_persists_workers(self, api_client, auth_headers, api_app):
        swarm_id = await _create_swarm(api_client, auth_headers)

        swarm = await api_app.state.db.get_swarm(swarm_id)
        assert swarm is not None
        assert swarm["repo"] == "agent-backbon"
        assert swarm["coding_agent_session"] == "agent-backbon"
        assert swarm["status"] == "active"
        assert len(swarm["workers"]) == 2
        assert {worker["name"] for worker in swarm["workers"]} == {"worker-a", "worker-b"}
        assert all(worker["status"] == "pending" for worker in swarm["workers"])

    async def test_create_swarm_requires_workers(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/swarms",
            headers=auth_headers,
            json={
                "repo": "agent-backbon",
                "coding_agent_session": "agent-backbon",
                "workers": [],
            },
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Swarm must include at least one worker"


class TestListAndGetSwarms:
    async def test_list_swarms_returns_progress_summary(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.get("/api/swarms", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["swarm_id"] == swarm_id
        assert data["items"][0]["worker_count"] == 2
        assert data["items"][0]["progress"]["total"] == 2
        assert data["items"][0]["progress"]["pending"] == 2

    async def test_get_swarm_returns_full_detail(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.get(f"/api/swarms/{swarm_id}", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["swarm_id"] == swarm_id
        assert len(data["workers"]) == 2
        assert data["workers"][0]["swarm_id"] == swarm_id

    async def test_completed_swarms_hidden_from_default_list(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)
        resp = await api_client.delete(f"/api/swarms/{swarm_id}", headers=auth_headers)
        assert resp.status_code == 200

        default_resp = await api_client.get("/api/swarms", headers=auth_headers)
        completed_resp = await api_client.get("/api/swarms?status=completed", headers=auth_headers)

        assert default_resp.status_code == 200
        assert default_resp.json()["total"] == 0
        assert completed_resp.status_code == 200
        assert completed_resp.json()["total"] == 1
        assert completed_resp.json()["items"][0]["status"] == "completed"


class TestUpdateAndCompleteSwarm:
    async def test_worker_status_update_sets_pr_and_completing_status(
        self, api_client, auth_headers
    ):
        swarm_id = await _create_swarm(
            api_client,
            auth_headers,
            workers=[
                {
                    "name": "worker-a",
                    "branch": "feature/swarm-a",
                    "worktree_path": "/tmp/swarm-a",
                    "session": "worker-a",
                }
            ],
        )

        resp = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/worker-a/status",
            headers=auth_headers,
            json={"status": "pr_created", "pr_number": 17},
        )
        assert resp.status_code == 200
        first = resp.json()
        assert first["workers"][0]["status"] == "pr_created"
        assert first["workers"][0]["pr_number"] == 17
        assert first["status"] == "active"

        resp2 = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/worker-a/status",
            headers=auth_headers,
            json={"status": "done"},
        )
        assert resp2.status_code == 200
        second = resp2.json()
        assert second["status"] == "completing"
        assert second["progress"]["done"] == 1
        assert second["progress"]["finished"] == 1

    async def test_delete_swarm_marks_completed(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.delete(f"/api/swarms/{swarm_id}", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["completed_at"] is not None

    async def test_missing_worker_returns_404(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/nope/status",
            headers=auth_headers,
            json={"status": "working"},
        )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Swarm worker not found"


class TestSwarmBroadcast:
    async def test_broadcast_delivers_to_all_workers(self, api_client, auth_headers, api_app):
        swarm_id = await _create_swarm(api_client, auth_headers)
        mock_svc = SimpleNamespace(safe_deliver=AsyncMock(side_effect=["delivered", "delivered"]))
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/swarms/{swarm_id}/broadcast",
                headers=auth_headers,
                json={"from_entity": "agent-backbon", "message": "Please sync your branches."},
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data == {"ok": True, "delivered": 2, "failed": 0, "total": 2}
        assert mock_svc.safe_deliver.await_count == 2
        first_call = mock_svc.safe_deliver.await_args_list[0]
        assert first_call.args[0] == "worker-a"
        assert "[via:swarm swarm:" in first_call.args[1]
        assert "Please sync your branches." in first_call.args[1]

    async def test_broadcast_counts_failed_deliveries(self, api_client, auth_headers, api_app):
        swarm_id = await _create_swarm(api_client, auth_headers)
        mock_svc = SimpleNamespace(
            safe_deliver=AsyncMock(side_effect=["delivered", "agent_working"])
        )
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/swarms/{swarm_id}/broadcast",
                headers=auth_headers,
                json={"from_entity": "agent-backbon", "message": "Stand by."},
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "delivered": 1, "failed": 1, "total": 2}


class TestSwarmAuth:
    async def test_requires_auth(self, api_client, api_key):
        resp = await api_client.get("/api/swarms")
        assert resp.status_code == 401
