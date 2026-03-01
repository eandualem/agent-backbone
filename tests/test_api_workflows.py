"""Tests for api/routes/workflows.py — workflow listing and execution endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_backbone.services.workflows import WorkflowEntry
from api.deps import get_workflows_service


@pytest.fixture
async def client(api_app):
    """Async test client bound to the api app."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_registry(*entries: WorkflowEntry) -> MagicMock:
    """Build a mock WorkflowRegistry populated with the given entries."""
    registry = MagicMock()
    registry.workflows = {e.name: e for e in entries}
    registry.get = lambda name: registry.workflows.get(name)
    return registry


def _make_mock_workflows_svc(registry=None, execute_result=None):
    """Create a mock WorkflowsService with get_registry and execute_steps."""
    svc = MagicMock()
    svc.get_registry = MagicMock(return_value=registry or _make_registry())
    svc.execute_steps = AsyncMock(return_value=execute_result or {"ok": True, "steps": []})
    return svc


# ---------------------------------------------------------------------------
# GET /api/workflows
# ---------------------------------------------------------------------------


class TestListWorkflows:
    async def test_returns_discovered_workflows(self, client, auth_headers, api_app):
        """Returns workflow entries from the registry."""
        mock_flow = AsyncMock(return_value="ok")
        entry = WorkflowEntry(
            name="test-workflow",
            description="A test workflow",
            module="flows.workflows.test",
            flow_fn=mock_flow,
        )
        mock_svc = _make_mock_workflows_svc(registry=_make_registry(entry))
        api_app.dependency_overrides[get_workflows_service] = lambda: mock_svc
        try:
            resp = await client.get("/api/workflows", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_workflows_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "test-workflow"
        assert data["items"][0]["description"] == "A test workflow"
        assert data["items"][0]["module"] == "flows.workflows.test"

    async def test_returns_empty_when_no_workflows(self, client, auth_headers, api_app):
        """Returns empty list when no workflows are registered."""
        mock_svc = _make_mock_workflows_svc(registry=_make_registry())
        api_app.dependency_overrides[get_workflows_service] = lambda: mock_svc
        try:
            resp = await client.get("/api/workflows", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_workflows_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_requires_auth(self, client, api_key):
        """Request without auth headers is rejected."""
        resp = await client.get("/api/workflows")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/workflows/{name}/run
# ---------------------------------------------------------------------------


class TestRunWorkflow:
    async def test_executes_workflow_successfully(self, client, auth_headers, api_app):
        """Runs the workflow flow_fn and returns the result."""
        mock_flow = AsyncMock(return_value="workflow completed")
        entry = WorkflowEntry(
            name="deploy-agents",
            description="Deploy all agents",
            module="flows.workflows.deploy",
            flow_fn=mock_flow,
        )
        mock_svc = _make_mock_workflows_svc(registry=_make_registry(entry))
        api_app.dependency_overrides[get_workflows_service] = lambda: mock_svc
        try:
            resp = await client.post("/api/workflows/deploy-agents/run", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_workflows_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["workflow"] == "deploy-agents"
        assert data["result"] == "workflow completed"
        mock_flow.assert_awaited_once()

    async def test_workflow_not_found_returns_404(self, client, auth_headers, api_app):
        """Returns 404 when the requested workflow name does not exist."""
        mock_svc = _make_mock_workflows_svc(registry=_make_registry())
        api_app.dependency_overrides[get_workflows_service] = lambda: mock_svc
        try:
            resp = await client.post("/api/workflows/nonexistent/run", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_workflows_service, None)

        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    async def test_workflow_execution_failure_returns_500(self, client, auth_headers, api_app):
        """Returns 500 when the workflow raises an exception."""
        mock_flow = AsyncMock(side_effect=RuntimeError("Something broke"))
        entry = WorkflowEntry(
            name="broken-workflow",
            description="This will fail",
            module="flows.workflows.broken",
            flow_fn=mock_flow,
        )
        mock_svc = _make_mock_workflows_svc(registry=_make_registry(entry))
        api_app.dependency_overrides[get_workflows_service] = lambda: mock_svc
        try:
            resp = await client.post("/api/workflows/broken-workflow/run", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_workflows_service, None)

        assert resp.status_code == 500
        assert "Something broke" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/workflows  (create JSON workflow)
# ---------------------------------------------------------------------------


class TestCreateWorkflow:
    async def test_create_workflow_success(self, client, auth_headers, tmp_path):
        """Creates a JSON workflow file and returns 201 with source=json."""
        with patch("api.routes.workflows._JSON_WORKFLOW_DIR", tmp_path):
            resp = await client.post(
                "/api/workflows",
                json={
                    "name": "my-wf",
                    "description": "Test workflow",
                    "steps": [{"action": "start", "session": "ike"}],
                },
                headers=auth_headers,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "my-wf"
        assert data["source"] == "json"
        assert (tmp_path / "my-wf.json").exists()

    async def test_create_duplicate_returns_409(self, client, auth_headers, tmp_path):
        """Returns 409 when a workflow with the same name already exists."""
        (tmp_path / "existing.json").write_text("{}")
        with patch("api.routes.workflows._JSON_WORKFLOW_DIR", tmp_path):
            resp = await client.post(
                "/api/workflows",
                json={
                    "name": "existing",
                    "description": "Dup",
                    "steps": [{"action": "stop", "session": "x"}],
                },
                headers=auth_headers,
            )
        assert resp.status_code == 409

    async def test_create_invalid_steps_returns_422(self, client, auth_headers, tmp_path):
        """Returns 422 when a step is missing required fields."""
        with patch("api.routes.workflows._JSON_WORKFLOW_DIR", tmp_path):
            resp = await client.post(
                "/api/workflows",
                json={
                    "name": "bad-wf",
                    "description": "",
                    "steps": [{"action": "start"}],  # missing session
                },
                headers=auth_headers,
            )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/workflows/{name}/run  (JSON workflow execution)
# ---------------------------------------------------------------------------


class TestRunJsonWorkflow:
    async def test_run_json_workflow(self, client, auth_headers, tmp_path, api_app):
        """Executes a JSON workflow via the workflow engine and returns result."""
        entry = WorkflowEntry(
            name="json-wf",
            description="A json workflow",
            source="json",
            steps=[{"action": "stop", "session": "test"}],
        )
        exec_result = {
            "ok": True,
            "steps": [{"action": "stop", "session": "test", "ok": True, "detail": "stopped"}],
        }
        mock_svc = _make_mock_workflows_svc(
            registry=_make_registry(entry), execute_result=exec_result
        )
        api_app.dependency_overrides[get_workflows_service] = lambda: mock_svc
        try:
            with patch("api.routes.workflows._JSON_WORKFLOW_DIR", tmp_path):
                resp = await client.post("/api/workflows/json-wf/run", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_workflows_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["workflow"] == "json-wf"
        assert "result" in data


# ---------------------------------------------------------------------------
# GET /api/workflows  (dual-mode listing: prefect + json)
# ---------------------------------------------------------------------------


class TestListDualMode:
    async def test_list_includes_source_and_last_run(self, client, auth_headers, api_app):
        """List endpoint includes source and last_run for both Prefect and JSON workflows."""
        prefect_entry = WorkflowEntry(
            name="prefect-wf",
            description="Prefect one",
            module="flows.workflows.test",
            flow_fn=AsyncMock(),
            source="prefect",
        )
        json_entry = WorkflowEntry(
            name="json-wf",
            description="JSON one",
            source="json",
            last_run="2026-02-16T10:00:00+00:00",
            steps=[{"action": "start", "session": "x"}],
        )
        registry = _make_registry(prefect_entry, json_entry)
        mock_svc = _make_mock_workflows_svc(registry=registry)
        api_app.dependency_overrides[get_workflows_service] = lambda: mock_svc
        try:
            resp = await client.get("/api/workflows", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_workflows_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        sources = {item["name"]: item["source"] for item in data["items"]}
        assert sources["prefect-wf"] == "prefect"
        assert sources["json-wf"] == "json"
        json_item = next(i for i in data["items"] if i["name"] == "json-wf")
        assert json_item["last_run"] == "2026-02-16T10:00:00+00:00"
        assert json_item["steps"] == [{"action": "start", "session": "x"}]
        prefect_item = next(i for i in data["items"] if i["name"] == "prefect-wf")
        assert prefect_item["steps"] == []
