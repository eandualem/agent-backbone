"""Tests for api/routes/prefect.py — Prefect flow runs proxy endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

_PREFECT = "api.routes.prefect"


class TestListFlowRuns:
    """Tests for GET /api/prefect/runs."""

    async def test_returns_flow_runs(self, api_client, auth_headers):
        """Proxies Prefect API and returns mapped flow runs."""
        raw_runs = [
            {
                "id": "abc-123",
                "name": "issue-dispatcher-run",
                "flow_id": "flow-1",
                "state": {"type": "COMPLETED", "message": "All states completed."},
                "start_time": "2026-02-15T10:00:00Z",
                "end_time": "2026-02-15T10:00:05Z",
            },
            {
                "id": "def-456",
                "name": "lifecycle-run",
                "flow_id": "flow-2",
                "state": {"type": "FAILED", "message": "Task failed."},
                "start_time": "2026-02-15T11:00:00Z",
                "end_time": "2026-02-15T11:00:02Z",
            },
        ]
        mock_response = httpx.Response(
            status_code=200,
            json=raw_runs,
            request=httpx.Request("POST", "http://localhost:4200/api/flow_runs/filter"),
        )

        with patch(f"{_PREFECT}.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/prefect/runs", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["items"][0]["id"] == "abc-123"
        assert data["items"][0]["state"] == "completed"
        assert data["items"][0]["duration"] == pytest.approx(5.0)
        assert data["items"][1]["state"] == "failed"
        assert data["items"][1]["duration"] == pytest.approx(2.0)

    async def test_state_mapping_running(self, api_client, auth_headers):
        """RUNNING state maps to 'running'."""
        raw_runs = [
            {
                "id": "run-1",
                "name": "running-flow",
                "flow_id": "f-1",
                "state": {"type": "RUNNING", "message": ""},
                "start_time": "2026-02-15T10:00:00Z",
                "end_time": None,
            },
        ]
        mock_response = httpx.Response(
            status_code=200,
            json=raw_runs,
            request=httpx.Request("POST", "http://test"),
        )

        with patch(f"{_PREFECT}.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/prefect/runs", headers=auth_headers)

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["state"] == "running"
        assert item["duration"] is None

    async def test_state_mapping_pending(self, api_client, auth_headers):
        """Unknown state types map to 'pending'."""
        raw_runs = [
            {
                "id": "run-2",
                "name": "pending-flow",
                "flow_id": "f-2",
                "state": {"type": "SCHEDULED", "message": ""},
                "start_time": None,
                "end_time": None,
            },
        ]
        mock_response = httpx.Response(
            status_code=200,
            json=raw_runs,
            request=httpx.Request("POST", "http://test"),
        )

        with patch(f"{_PREFECT}.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/prefect/runs", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["items"][0]["state"] == "pending"

    async def test_limit_parameter(self, api_client, auth_headers):
        """Limit parameter is passed to Prefect API."""
        mock_response = httpx.Response(
            status_code=200,
            json=[],
            request=httpx.Request("POST", "http://test"),
        )

        with patch(f"{_PREFECT}.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/prefect/runs?limit=5", headers=auth_headers)

        assert resp.status_code == 200
        call_args = mock_client.post.call_args
        assert call_args[1]["json"]["limit"] == 5

    async def test_prefect_unavailable_returns_502(self, api_client, auth_headers):
        """When Prefect API is unreachable, returns 502."""
        with patch(f"{_PREFECT}.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/prefect/runs", headers=auth_headers)

        assert resp.status_code == 502
        assert "Prefect API unavailable" in resp.json()["detail"]

    async def test_crashed_and_cancelled_map_to_failed(self, api_client, auth_headers):
        """CRASHED and CANCELLED states both map to 'failed'."""
        raw_runs = [
            {
                "id": "r1",
                "name": "crashed",
                "flow_id": "f",
                "state": {"type": "CRASHED", "message": ""},
                "start_time": None,
                "end_time": None,
            },
            {
                "id": "r2",
                "name": "cancelled",
                "flow_id": "f",
                "state": {"type": "CANCELLED", "message": ""},
                "start_time": None,
                "end_time": None,
            },
        ]
        mock_response = httpx.Response(
            status_code=200,
            json=raw_runs,
            request=httpx.Request("POST", "http://test"),
        )

        with patch(f"{_PREFECT}.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/prefect/runs", headers=auth_headers)

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items[0]["state"] == "failed"
        assert items[1]["state"] == "failed"
