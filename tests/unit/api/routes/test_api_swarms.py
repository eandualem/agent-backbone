"""Tests for swarm registry API routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_backbone.api.deps import get_delivery_service


async def _create_swarm(api_client, auth_headers, **overrides) -> str:
    payload = {
        "repo": "agent-backbon",
        "task_id": "750",
        "coding_agent_session": "agent-backbon",
        "workers": [
            {
                "name": "lead-worker",
                "role": "lead",
                "branch": "swarm/750/lead-worker",
                "worktree_path": "/tmp/lead-worker",
                "session": "lead-worker",
            },
            {
                "name": "tester-worker",
                "role": "tester",
                "branch": "swarm/750/tester-worker",
                "worktree_path": "/tmp/tester-worker",
                "session": "tester-worker",
            },
        ],
    }
    payload.update(overrides)
    resp = await api_client.post("/api/swarms", headers=auth_headers, json=payload)
    assert resp.status_code == 201
    return resp.json()["swarm_id"]


class TestCreateSwarm:
    async def test_create_swarm_persists_roles_and_created_phase(
        self, api_client, auth_headers, api_app
    ):
        swarm_id = await _create_swarm(api_client, auth_headers)

        swarm = await api_app.state.db.get_swarm(swarm_id)
        assert swarm is not None
        assert swarm["repo"] == "agent-backbon"
        assert swarm["coding_agent_session"] == "agent-backbon"
        assert swarm["phase"] == "created"
        assert len(swarm["workers"]) == 2
        assert {worker["role"] for worker in swarm["workers"]} == {"lead", "tester"}
        assert swarm["phase_history"][0]["to_phase"] == "created"

    async def test_create_swarm_requires_roles(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/swarms",
            headers=auth_headers,
            json={
                "repo": "agent-backbon",
                "coding_agent_session": "agent-backbon",
                "workers": [
                    {
                        "name": "worker-a",
                        "branch": "feature/swarm-a",
                        "worktree_path": "/tmp/swarm-a",
                        "session": "worker-a",
                    }
                ],
            },
        )

        assert resp.status_code == 422

    async def test_create_swarm_rejects_multiple_leads(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/swarms",
            headers=auth_headers,
            json={
                "repo": "agent-backbon",
                "coding_agent_session": "agent-backbon",
                "workers": [
                    {
                        "name": "lead-a",
                        "role": "lead",
                        "branch": "swarm/764/lead-a",
                        "worktree_path": "/tmp/lead-a",
                        "session": "lead-a",
                    },
                    {
                        "name": "lead-b",
                        "role": "lead",
                        "branch": "swarm/764/lead-b",
                        "worktree_path": "/tmp/lead-b",
                        "session": "lead-b",
                    },
                ],
            },
        )

        assert resp.status_code == 400
        assert "one lead" in resp.json()["detail"]


class TestListAndGetSwarms:
    async def test_list_swarms_returns_progress_summary(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.get("/api/swarms", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["swarm_id"] == swarm_id
        assert data["items"][0]["phase"] == "created"
        assert data["items"][0]["worker_count"] == 2
        assert data["items"][0]["progress"]["pending"] == 2

    async def test_get_swarm_returns_grouped_workers_and_history(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.get(f"/api/swarms/{swarm_id}", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["swarm_id"] == swarm_id
        assert len(data["workers"]) == 2
        assert len(data["workers_by_role"]["lead"]) == 1
        assert len(data["workers_by_role"]["tester"]) == 1
        assert data["phase_history"][0]["to_phase"] == "created"


class TestLifecycle:
    async def test_phase_update_validates_transition(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        bad_resp = await api_client.post(
            f"/api/swarms/{swarm_id}/phase",
            headers=auth_headers,
            json={"phase": "merged"},
        )
        assert bad_resp.status_code == 409
        assert "Illegal phase transition" in bad_resp.json()["detail"]

        good_resp = await api_client.post(
            f"/api/swarms/{swarm_id}/phase",
            headers=auth_headers,
            json={"phase": "planning"},
        )
        assert good_resp.status_code == 200
        assert good_resp.json()["phase"] == "planning"

    async def test_complete_worker_auto_promotes_to_validating(self, api_client, auth_headers):
        swarm_id = await _create_swarm(
            api_client,
            auth_headers,
            workers=[
                {
                    "name": "coder-worker",
                    "role": "coder",
                    "branch": "swarm/750/coder-worker",
                    "worktree_path": "/tmp/coder-worker",
                    "session": "coder-worker",
                }
            ],
        )

        planning = await api_client.post(
            f"/api/swarms/{swarm_id}/phase",
            headers=auth_headers,
            json={"phase": "planning"},
        )
        assert planning.status_code == 200
        working = await api_client.post(
            f"/api/swarms/{swarm_id}/phase",
            headers=auth_headers,
            json={"phase": "working"},
        )
        assert working.status_code == 200

        resp = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/coder-worker/status",
            headers=auth_headers,
            json={"status": "pr_created", "pr_number": 17},
        )
        assert resp.status_code == 200
        assert resp.json()["workers"][0]["status"] == "pr_created"
        assert resp.json()["phase"] == "working"

        resp2 = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/coder-worker/complete",
            headers=auth_headers,
            json={"status": "done", "summary": "Opened PR #17", "pr_number": 17},
        )
        assert resp2.status_code == 200
        second = resp2.json()
        assert second["phase"] == "validating"
        assert second["progress"]["done"] == 1
        assert second["workers"][0]["summary"] == "Opened PR #17"
        assert second["workers"][0]["completed_at"] is not None

    async def test_status_endpoint_rejects_terminal_updates(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/lead-worker/status",
            headers=auth_headers,
            json={"status": "done"},
        )

        assert resp.status_code == 400
        assert "completion endpoint" in resp.json()["detail"]

    async def test_delete_marks_merged_swarm_cleaned_up(self, api_client, auth_headers):
        swarm_id = await _create_swarm(
            api_client,
            auth_headers,
            workers=[
                {
                    "name": "lead-worker",
                    "role": "lead",
                    "branch": "swarm/750/lead-worker",
                    "worktree_path": "/tmp/lead-worker",
                    "session": "lead-worker",
                }
            ],
        )

        not_ready = await api_client.delete(f"/api/swarms/{swarm_id}", headers=auth_headers)
        assert not_ready.status_code == 409

        for phase in ("planning", "working"):
            resp = await api_client.post(
                f"/api/swarms/{swarm_id}/phase",
                headers=auth_headers,
                json={"phase": phase},
            )
            assert resp.status_code == 200

        done = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/lead-worker/complete",
            headers=auth_headers,
            json={"status": "done", "summary": "Ready for review"},
        )
        assert done.status_code == 200
        assert done.json()["phase"] == "validating"

        for phase in ("pr_open", "awaiting_review", "merged"):
            resp = await api_client.post(
                f"/api/swarms/{swarm_id}/phase",
                headers=auth_headers,
                json={"phase": phase},
            )
            assert resp.status_code == 200

        cleanup = await api_client.delete(f"/api/swarms/{swarm_id}", headers=auth_headers)
        assert cleanup.status_code == 200
        assert cleanup.json()["phase"] == "cleaned_up"

        default_resp = await api_client.get("/api/swarms", headers=auth_headers)
        cleaned_resp = await api_client.get(
            "/api/swarms?phase=cleaned_up",
            headers=auth_headers,
        )
        assert default_resp.status_code == 200
        assert default_resp.json()["total"] == 0
        assert cleaned_resp.status_code == 200
        assert cleaned_resp.json()["total"] == 1


class TestAssignmentDiscipline:
    async def test_worker_status_requires_active_assignment_in_collaborative_swarm(
        self, api_client, auth_headers
    ):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/tester-worker/status",
            headers=auth_headers,
            json={"status": "started"},
        )

        assert resp.status_code == 400
        assert "active assignment" in resp.json()["detail"]

    async def test_lead_cannot_enter_implementation_status(self, api_client, auth_headers):
        swarm_id = await _create_swarm(api_client, auth_headers)

        resp = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/lead-worker/status",
            headers=auth_headers,
            json={"status": "working"},
        )

        assert resp.status_code == 400
        assert "Lead workers cannot enter implementation statuses" in resp.json()["detail"]

    async def test_assignment_dispatch_persists_and_unblocks_worker(
        self, api_client, auth_headers, api_app
    ):
        swarm_id = await _create_swarm(api_client, auth_headers)
        mock_svc = SimpleNamespace(safe_deliver=AsyncMock(return_value="delivered"))
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            assign_resp = await api_client.post(
                f"/api/swarms/{swarm_id}/assignments",
                headers=auth_headers,
                json={
                    "worker_name": "tester-worker",
                    "from_entity": "lead-worker",
                    "summary": "Run API regression coverage.",
                    "file_paths": ["tests/unit/api/routes/test_api_swarms.py"],
                },
            )
            detail_resp = await api_client.get(f"/api/swarms/{swarm_id}", headers=auth_headers)
            list_resp = await api_client.get(
                f"/api/swarms/{swarm_id}/assignments",
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert assign_resp.status_code == 200
        assignment = assign_resp.json()["assignment"]
        assert assignment["worker_name"] == "tester-worker"
        assert assignment["status"] == "active"
        assert assignment["file_paths"] == ["tests/unit/api/routes/test_api_swarms.py"]
        assert mock_svc.safe_deliver.await_count == 1

        assert detail_resp.status_code == 200
        assert len(detail_resp.json()["assignments"]) == 1
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] == 1

        status_resp = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/tester-worker/status",
            headers=auth_headers,
            json={"status": "started"},
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["workers"][1]["status"] == "started"

    async def test_assignment_rejects_lead_target(self, api_client, auth_headers, api_app):
        swarm_id = await _create_swarm(api_client, auth_headers)
        mock_svc = SimpleNamespace(safe_deliver=AsyncMock(return_value="delivered"))
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/swarms/{swarm_id}/assignments",
                headers=auth_headers,
                json={
                    "worker_name": "lead-worker",
                    "from_entity": "lead-worker",
                    "summary": "Do the implementation.",
                    "file_paths": ["src/agent_backbone/api/routes/swarms.py"],
                },
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 409
        assert "Lead workers cannot receive implementation assignments" in resp.json()["detail"]

    async def test_assignment_rejects_file_conflicts(self, api_client, auth_headers, api_app):
        swarm_id = await _create_swarm(
            api_client,
            auth_headers,
            workers=[
                {
                    "name": "lead-worker",
                    "role": "lead",
                    "branch": "swarm/764/lead-worker",
                    "worktree_path": "/tmp/lead-worker",
                    "session": "lead-worker",
                },
                {
                    "name": "coder-a",
                    "role": "coder",
                    "branch": "swarm/764/coder-a",
                    "worktree_path": "/tmp/coder-a",
                    "session": "coder-a",
                },
                {
                    "name": "coder-b",
                    "role": "coder",
                    "branch": "swarm/764/coder-b",
                    "worktree_path": "/tmp/coder-b",
                    "session": "coder-b",
                },
            ],
        )
        mock_svc = SimpleNamespace(safe_deliver=AsyncMock(return_value="delivered"))
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            first = await api_client.post(
                f"/api/swarms/{swarm_id}/assignments",
                headers=auth_headers,
                json={
                    "worker_name": "coder-a",
                    "from_entity": "lead-worker",
                    "summary": "Own the delivery router changes.",
                    "file_paths": ["src/agent_backbone/api/routes/swarms.py"],
                },
            )
            second = await api_client.post(
                f"/api/swarms/{swarm_id}/assignments",
                headers=auth_headers,
                json={
                    "worker_name": "coder-b",
                    "from_entity": "lead-worker",
                    "summary": "Also edit the same route.",
                    "file_paths": ["src/agent_backbone/api/routes/swarms.py"],
                },
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert first.status_code == 200
        assert second.status_code == 409
        assert "already assigned" in second.json()["detail"]

    async def test_reassigning_same_worker_supersedes_previous_assignment(
        self, api_client, auth_headers, api_app
    ):
        swarm_id = await _create_swarm(api_client, auth_headers)
        mock_svc = SimpleNamespace(safe_deliver=AsyncMock(return_value="delivered"))
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            first = await api_client.post(
                f"/api/swarms/{swarm_id}/assignments",
                headers=auth_headers,
                json={
                    "worker_name": "tester-worker",
                    "from_entity": "lead-worker",
                    "summary": "Check API coverage.",
                    "file_paths": ["tests/unit/api/routes/test_api_swarms.py"],
                },
            )
            second = await api_client.post(
                f"/api/swarms/{swarm_id}/assignments",
                headers=auth_headers,
                json={
                    "worker_name": "tester-worker",
                    "from_entity": "lead-worker",
                    "summary": "Switch to DB regression coverage.",
                    "file_paths": ["tests/unit/services/database/test_alembic.py"],
                },
            )
            list_resp = await api_client.get(
                f"/api/swarms/{swarm_id}/assignments",
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert first.status_code == 200
        assert second.status_code == 200
        items = list_resp.json()["items"]
        assert [item["status"] for item in items] == ["superseded", "active"]

    async def test_non_collaborative_swarms_keep_existing_status_behavior(
        self, api_client, auth_headers
    ):
        swarm_id = await _create_swarm(
            api_client,
            auth_headers,
            workers=[
                {
                    "name": "coder-worker",
                    "role": "coder",
                    "branch": "swarm/764/coder-worker",
                    "worktree_path": "/tmp/coder-worker",
                    "session": "coder-worker",
                }
            ],
        )

        resp = await api_client.post(
            f"/api/swarms/{swarm_id}/workers/coder-worker/status",
            headers=auth_headers,
            json={"status": "started"},
        )

        assert resp.status_code == 200
        assert resp.json()["workers"][0]["status"] == "started"


class TestSwarmMessaging:
    async def test_broadcast_logs_message(self, api_client, auth_headers, api_app):
        swarm_id = await _create_swarm(api_client, auth_headers)
        mock_svc = SimpleNamespace(safe_deliver=AsyncMock(side_effect=["delivered", "delivered"]))
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/swarms/{swarm_id}/broadcast",
                headers=auth_headers,
                json={"from_entity": "agent-backbon", "message": "Please sync your branches."},
            )
            messages_resp = await api_client.get(
                f"/api/swarms/{swarm_id}/messages",
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["message_id"] is not None
        assert mock_svc.safe_deliver.await_count == 2

        assert messages_resp.status_code == 200
        messages = messages_resp.json()
        assert messages["total"] == 1
        assert messages["items"][0]["target_kind"] == "broadcast"
        assert messages["items"][0]["delivered"] == 2

    async def test_role_message_targets_matching_workers(self, api_client, auth_headers, api_app):
        swarm_id = await _create_swarm(api_client, auth_headers)
        mock_svc = SimpleNamespace(
            safe_deliver=AsyncMock(side_effect=["agent_working"])
        )
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/swarms/{swarm_id}/message",
                headers=auth_headers,
                json={
                    "role": "tester",
                    "from_entity": "agent-backbon",
                    "message": "Run the regression suite.",
                },
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": False,
            "message_id": 1,
            "delivered": 0,
            "failed": 1,
            "total": 1,
        }
        first_call = mock_svc.safe_deliver.await_args_list[0]
        assert first_call.args[0] == "tester-worker"
        assert "Target: role:tester" in first_call.args[1]

    async def test_message_requires_exactly_one_target(self, api_client, auth_headers, api_app):
        swarm_id = await _create_swarm(api_client, auth_headers)
        mock_svc = SimpleNamespace(safe_deliver=AsyncMock(return_value="delivered"))
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/swarms/{swarm_id}/message",
                headers=auth_headers,
                json={
                    "role": "tester",
                    "worker_name": "tester-worker",
                    "from_entity": "agent-backbon",
                    "message": "Invalid request.",
                },
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 422


class TestSwarmAuth:
    async def test_requires_auth(self, api_client, api_key):
        resp = await api_client.get("/api/swarms")
        assert resp.status_code == 401
