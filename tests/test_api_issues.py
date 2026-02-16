"""Tests for API issues routes (api/routes/issues.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from api.deps import get_db, get_github
from src.models import CommentData, IssueData, ParsedLabels
from src.persistence import BackboneDB

# --- Fixtures ---


@pytest.fixture
def sample_issues():
    """Two sample issues for list tests."""
    return [
        IssueData(
            number=42,
            title="[task] Update config",
            state="open",
            labels=ParsedLabels(sender="leo", targets=["ike"], issue_type="task"),
            html_url="https://github.com/eandualem/orchestration/issues/42",
        ),
        IssueData(
            number=43,
            title="[bug] Fix routing",
            state="open",
            labels=ParsedLabels(sender="ada", targets=["feynman"], issue_type="bug"),
            html_url="https://github.com/eandualem/orchestration/issues/43",
        ),
    ]


@pytest.fixture
def sample_comments():
    """Sample comments with and without from-tags."""
    return [
        CommentData(id=1, body="[from:ike] Done", user_login="eandualem"),
        CommentData(id=2, body="No tag here", user_login="eandualem"),
    ]


@pytest.fixture
def mock_github(sample_issues, sample_comments):
    """Mock GitHubClient with default return values."""
    mock = AsyncMock()
    mock.list_issues.return_value = sample_issues
    mock.get_issue.return_value = sample_issues[0]
    mock.list_comments.return_value = sample_comments
    mock.get_sub_issues.return_value = []
    mock.create_issue.return_value = IssueData(
        number=99,
        title="[task] New issue",
        state="open",
        labels=ParsedLabels(sender="leo", targets=["ike"], issue_type="task"),
        html_url="https://github.com/eandualem/orchestration/issues/99",
    )
    mock.add_comment.return_value = CommentData(
        id=10, body="[from:ike] Acknowledged", user_login="eandualem"
    )
    mock.update_issue.return_value = IssueData(
        number=42,
        title="[task] Update config",
        state="closed",
        labels=ParsedLabels(sender="leo", targets=["ike"], issue_type="task"),
        html_url="https://github.com/eandualem/orchestration/issues/42",
    )
    return mock


@pytest.fixture
async def issues_client(api_app, auth_headers, mock_github):
    """Async test client with GitHub mock and in-memory DB overridden."""

    async def override_github():
        yield mock_github

    async def override_db():
        async with BackboneDB(":memory:") as db:
            yield db

    api_app.dependency_overrides[get_github] = override_github
    api_app.dependency_overrides[get_db] = override_db

    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    api_app.dependency_overrides.clear()


# --- Tests ---


class TestListIssues:
    async def test_list_issues_returns_items_with_scores(
        self, issues_client, auth_headers, mock_github
    ):
        resp = await issues_client.get("/api/issues", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        # Verify structure of first item
        item = data["items"][0]
        assert item["number"] == 42
        assert item["title"] == "[task] Update config"
        assert item["state"] == "open"
        assert item["labels"]["sender"] == "leo"
        assert item["labels"]["targets"] == ["ike"]
        assert item["labels"]["issue_type"] == "task"
        assert "priority_score" in item
        # Default call uses state=open, no labels
        mock_github.list_issues.assert_called_once_with(state="open", labels=[])

    async def test_list_issues_filter_by_for_entity(self, issues_client, auth_headers, mock_github):
        resp = await issues_client.get("/api/issues?for_entity=ike", headers=auth_headers)
        assert resp.status_code == 200
        mock_github.list_issues.assert_called_once_with(state="open", labels=["for:ike"])

    async def test_list_issues_filter_by_from_entity(
        self, issues_client, auth_headers, mock_github
    ):
        resp = await issues_client.get("/api/issues?from_entity=leo", headers=auth_headers)
        assert resp.status_code == 200
        mock_github.list_issues.assert_called_once_with(state="open", labels=["from:leo"])

    async def test_list_issues_filter_by_type(self, issues_client, auth_headers, mock_github):
        resp = await issues_client.get("/api/issues?type=bug", headers=auth_headers)
        assert resp.status_code == 200
        mock_github.list_issues.assert_called_once_with(state="open", labels=["bug"])

    async def test_list_issues_filter_by_state(self, issues_client, auth_headers, mock_github):
        resp = await issues_client.get("/api/issues?state=closed", headers=auth_headers)
        assert resp.status_code == 200
        mock_github.list_issues.assert_called_once_with(state="closed", labels=[])

    async def test_list_issues_requires_auth(self, issues_client):
        resp = await issues_client.get("/api/issues")
        assert resp.status_code == 401


class TestGetIssue:
    async def test_get_issue_by_number(self, issues_client, auth_headers, mock_github):
        resp = await issues_client.get("/api/issues/42", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["number"] == 42
        assert data["title"] == "[task] Update config"
        assert data["html_url"] == "https://github.com/eandualem/orchestration/issues/42"
        assert "priority_score" in data
        mock_github.get_issue.assert_called_once_with(42)

    async def test_get_issue_not_found(self, issues_client, auth_headers, mock_github):
        mock_github.get_issue.side_effect = Exception("Not found")
        resp = await issues_client.get("/api/issues/999", headers=auth_headers)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


class TestListComments:
    async def test_list_comments_parses_from_tag(self, issues_client, auth_headers, mock_github):
        resp = await issues_client.get("/api/issues/42/comments", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        items = data["items"]
        # First comment has [from:ike] tag
        assert items[0]["id"] == 1
        assert items[0]["body"] == "[from:ike] Done"
        assert items[0]["from_entity"] == "ike"
        # Second comment has no from-tag
        assert items[1]["id"] == 2
        assert items[1]["from_entity"] is None
        mock_github.list_comments.assert_called_once_with(42)


class TestGetDependencies:
    async def test_dependencies_empty(self, issues_client, auth_headers, mock_github):
        resp = await issues_client.get("/api/issues/42/dependencies", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["sub_issues"] == []
        assert data["parents"] == []
        mock_github.get_sub_issues.assert_called_once_with(42)

    async def test_dependencies_with_sub_issues(self, issues_client, auth_headers, mock_github):
        sub = IssueData(
            number=50,
            title="[task] Sub-task",
            state="open",
            labels=ParsedLabels(sender="ike", targets=["ada"], issue_type="task"),
            html_url="https://github.com/eandualem/orchestration/issues/50",
        )
        mock_github.get_sub_issues.return_value = [sub]
        resp = await issues_client.get("/api/issues/42/dependencies", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sub_issues"]) == 1
        assert data["sub_issues"][0]["number"] == 50


class TestCreateIssue:
    async def test_create_issue_success(self, issues_client, auth_headers, mock_github):
        payload = {
            "title": "[task] New issue",
            "body": "## Context\nTest",
            "labels": ["from:leo", "for:ike", "task"],
        }
        resp = await issues_client.post("/api/issues", json=payload, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["number"] == 99
        assert data["title"] == "[task] New issue"
        mock_github.create_issue.assert_called_once_with(
            "[task] New issue", "## Context\nTest", ["from:leo", "for:ike", "task"]
        )

    async def test_create_issue_missing_title(self, issues_client, auth_headers):
        payload = {"body": "no title"}
        resp = await issues_client.post("/api/issues", json=payload, headers=auth_headers)
        assert resp.status_code == 400
        assert "title" in resp.json()["detail"].lower()

    async def test_create_issue_empty_title(self, issues_client, auth_headers):
        payload = {"title": "", "body": "empty title"}
        resp = await issues_client.post("/api/issues", json=payload, headers=auth_headers)
        assert resp.status_code == 400


class TestAddComment:
    async def test_add_comment_success(self, issues_client, auth_headers, mock_github):
        payload = {"body": "[from:ike] Acknowledged"}
        resp = await issues_client.post(
            "/api/issues/42/comment", json=payload, headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 10
        assert data["body"] == "[from:ike] Acknowledged"
        assert data["from_entity"] == "ike"
        mock_github.add_comment.assert_called_once_with(42, "[from:ike] Acknowledged")

    async def test_add_comment_missing_body(self, issues_client, auth_headers):
        payload = {"something": "else"}
        resp = await issues_client.post(
            "/api/issues/42/comment", json=payload, headers=auth_headers
        )
        assert resp.status_code == 400
        assert "body" in resp.json()["detail"].lower()

    async def test_add_comment_empty_body(self, issues_client, auth_headers):
        payload = {"body": ""}
        resp = await issues_client.post(
            "/api/issues/42/comment", json=payload, headers=auth_headers
        )
        assert resp.status_code == 400


class TestUpdateIssue:
    async def test_update_issue_close(self, issues_client, auth_headers, mock_github):
        payload = {"state": "closed"}
        resp = await issues_client.patch("/api/issues/42", json=payload, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["number"] == 42
        assert data["state"] == "closed"
        mock_github.update_issue.assert_called_once_with(42, "closed")

    async def test_update_issue_missing_state(self, issues_client, auth_headers):
        payload = {"title": "new title"}
        resp = await issues_client.patch("/api/issues/42", json=payload, headers=auth_headers)
        assert resp.status_code == 400
        assert "state" in resp.json()["detail"].lower()
