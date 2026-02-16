"""Tests for api/routes/workflows.py — workflow listing and execution endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.workflow_registry import WorkflowEntry


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


# ---------------------------------------------------------------------------
# GET /api/workflows
# ---------------------------------------------------------------------------


class TestListWorkflows:
    async def test_returns_discovered_workflows(self, client, auth_headers):
        """Returns workflow entries from the registry."""
        mock_flow = AsyncMock(return_value="ok")
        entry = WorkflowEntry(
            name="test-workflow",
            description="A test workflow",
            module="flows.workflows.test",
            flow_fn=mock_flow,
        )
        registry = _make_registry(entry)

        with patch("api.routes.workflows._get_registry", return_value=registry):
            resp = await client.get("/api/workflows", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "test-workflow"
        assert data["items"][0]["description"] == "A test workflow"
        assert data["items"][0]["module"] == "flows.workflows.test"

    async def test_returns_empty_when_no_workflows(self, client, auth_headers):
        """Returns empty list when no workflows are registered."""
        registry = _make_registry()

        with patch("api.routes.workflows._get_registry", return_value=registry):
            resp = await client.get("/api/workflows", headers=auth_headers)

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
    async def test_executes_workflow_successfully(self, client, auth_headers):
        """Runs the workflow flow_fn and returns the result."""
        mock_flow = AsyncMock(return_value="workflow completed")
        entry = WorkflowEntry(
            name="deploy-agents",
            description="Deploy all agents",
            module="flows.workflows.deploy",
            flow_fn=mock_flow,
        )
        registry = _make_registry(entry)

        with patch("api.routes.workflows._get_registry", return_value=registry):
            resp = await client.post("/api/workflows/deploy-agents/run", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["workflow"] == "deploy-agents"
        assert data["result"] == "workflow completed"
        mock_flow.assert_awaited_once()

    async def test_workflow_not_found_returns_404(self, client, auth_headers):
        """Returns 404 when the requested workflow name does not exist."""
        registry = _make_registry()  # empty registry

        with patch("api.routes.workflows._get_registry", return_value=registry):
            resp = await client.post("/api/workflows/nonexistent/run", headers=auth_headers)

        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    async def test_workflow_execution_failure_returns_500(self, client, auth_headers):
        """Returns 500 when the workflow raises an exception."""
        mock_flow = AsyncMock(side_effect=RuntimeError("Something broke"))
        entry = WorkflowEntry(
            name="broken-workflow",
            description="This will fail",
            module="flows.workflows.broken",
            flow_fn=mock_flow,
        )
        registry = _make_registry(entry)

        with patch("api.routes.workflows._get_registry", return_value=registry):
            resp = await client.post("/api/workflows/broken-workflow/run", headers=auth_headers)

        assert resp.status_code == 500
        assert "Something broke" in resp.json()["detail"]
