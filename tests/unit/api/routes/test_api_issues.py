"""Tests for API issues routes (api/routes/issues.py).

These tests exercise the canonical issue projection endpoints backed by IssueService.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.api.deps import get_db, get_github, get_issue_service
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubServiceError
from agent_backbone.services.issues import IssueService

# --- Helpers ---

_OWNER = "eandualem"
_REPO = "orchestration"

_SAMPLE_SUMMARY_1 = {
    "repo_full_name": f"{_OWNER}/{_REPO}",
    "number": 42,
    "title": "[task] Update config",
    "state": "open",
    "html_url": f"https://github.com/{_OWNER}/{_REPO}/issues/42",
    "labels": {
        "sender": "leo",
        "targets": ["ike"],
        "issue_type": "task",
        "priority": "",
        "all_labels": ["from:leo", "for:ike", "task"],
    },
    "priority_score": 10.0,
    "created_at": "2026-03-01T00:00:00Z",
    "updated_at": "2026-03-02T00:00:00Z",
    "comment_count": 2,
    "user_login": "eandualem",
}

_SAMPLE_SUMMARY_2 = {
    "repo_full_name": f"{_OWNER}/{_REPO}",
    "number": 43,
    "title": "[bug] Fix routing",
    "state": "open",
    "html_url": f"https://github.com/{_OWNER}/{_REPO}/issues/43",
    "labels": {
        "sender": "ada",
        "targets": ["feynman"],
        "issue_type": "bug",
        "priority": "",
        "all_labels": ["from:ada", "for:feynman", "bug"],
    },
    "priority_score": 8.0,
    "created_at": "2026-03-01T00:00:00Z",
    "updated_at": "2026-03-02T00:00:00Z",
    "comment_count": 0,
    "user_login": "eandualem",
}

_SAMPLE_DETAIL_1 = {**_SAMPLE_SUMMARY_1, "body": "## Context\nSome detail"}

_SAMPLE_COMMENT_1 = {
    "id": 1,
    "body": "[from:ike] Done",
    "user_login": "eandualem",
    "from_entity": "ike",
    "created_at": "2026-03-01T00:00:00Z",
    "updated_at": "2026-03-01T00:00:00Z",
}
_SAMPLE_COMMENT_2 = {
    "id": 2,
    "body": "No tag here",
    "user_login": "eandualem",
    "from_entity": None,
    "created_at": "2026-03-01T01:00:00Z",
    "updated_at": "2026-03-01T01:00:00Z",
}

# --- Fixtures ---


@pytest.fixture
def mock_issue_service():
    """Mock IssueService with default return values."""
    mock = AsyncMock(spec=IssueService)
    mock.list_issues.return_value = (
        [_SAMPLE_SUMMARY_1, _SAMPLE_SUMMARY_2],
        2,
    )
    mock.get_issue.return_value = _SAMPLE_DETAIL_1
    mock.list_comments.return_value = [_SAMPLE_COMMENT_1, _SAMPLE_COMMENT_2]
    mock.count_open_issues.return_value = 2
    mock.create_issue.return_value = {
        "repo_full_name": f"{_OWNER}/{_REPO}",
        "number": 99,
        "title": "[task] New issue",
        "state": "open",
        "html_url": f"https://github.com/{_OWNER}/{_REPO}/issues/99",
        "labels": {
            "sender": "leo",
            "targets": ["ike"],
            "issue_type": "task",
            "priority": "",
            "all_labels": ["from:leo", "for:ike", "task"],
        },
        "priority_score": 10.0,
        "created_at": "2026-03-10T00:00:00Z",
        "updated_at": "2026-03-10T00:00:00Z",
        "comment_count": 0,
        "user_login": "eandualem",
    }
    mock.add_comment.return_value = {
        "id": 10,
        "body": "[from:ike] Acknowledged",
        "user_login": "eandualem",
        "from_entity": "ike",
        "created_at": "2026-03-10T00:00:00Z",
        "updated_at": "2026-03-10T00:00:00Z",
    }
    mock.update_issue_state.return_value = {
        "repo_full_name": f"{_OWNER}/{_REPO}",
        "number": 42,
        "title": "[task] Update config",
        "state": "closed",
        "html_url": f"https://github.com/{_OWNER}/{_REPO}/issues/42",
        "labels": {
            "sender": "leo",
            "targets": ["ike"],
            "issue_type": "task",
            "priority": "",
            "all_labels": ["from:leo", "for:ike", "task"],
        },
        "priority_score": 10.0,
        "created_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-10T00:00:00Z",
        "comment_count": 2,
        "user_login": "eandualem",
    }
    return mock


@pytest.fixture
def mock_github():
    """Mock GitHubClient for dependency endpoints."""
    mock = AsyncMock()
    mock.get_sub_issues.return_value = []
    return mock


@pytest.fixture
async def issues_client(api_app, auth_headers, mock_issue_service, mock_github):
    """Async test client with IssueService mock and in-memory DB overridden."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    db = BackboneDB(engine)
    await db.start()

    api_app.dependency_overrides[get_issue_service] = lambda: mock_issue_service
    api_app.dependency_overrides[get_github] = lambda: mock_github
    api_app.dependency_overrides[get_db] = lambda: db

    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    api_app.dependency_overrides.clear()
    db._engine = None
    await engine.dispose()


# --- Tests ---


class TestListIssues:
    async def test_list_issues_returns_items_with_scores(
        self, issues_client, auth_headers, mock_issue_service
    ):
        resp = await issues_client.get("/api/issues", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        item = data["items"][0]
        assert item["number"] == 42
        assert item["title"] == "[task] Update config"
        assert item["state"] == "open"
        assert item["labels"]["sender"] == "leo"
        assert item["labels"]["targets"] == ["ike"]
        assert item["labels"]["issue_type"] == "task"
        assert "priority_score" in item
        mock_issue_service.list_issues.assert_called_once_with(
            state="open",
            repo_full_name=None,
            for_entity=None,
            from_entity=None,
            issue_type=None,
            label=None,
            page=1,
            per_page=50,
        )

    async def test_list_issues_filter_by_for_entity(
        self, issues_client, auth_headers, mock_issue_service
    ):
        resp = await issues_client.get("/api/issues?for_entity=ike", headers=auth_headers)
        assert resp.status_code == 200
        mock_issue_service.list_issues.assert_called_once_with(
            state="open",
            repo_full_name=None,
            for_entity="ike",
            from_entity=None,
            issue_type=None,
            label=None,
            page=1,
            per_page=50,
        )

    async def test_list_issues_filter_by_from_entity(
        self, issues_client, auth_headers, mock_issue_service
    ):
        resp = await issues_client.get("/api/issues?from_entity=leo", headers=auth_headers)
        assert resp.status_code == 200
        mock_issue_service.list_issues.assert_called_once_with(
            state="open",
            repo_full_name=None,
            for_entity=None,
            from_entity="leo",
            issue_type=None,
            label=None,
            page=1,
            per_page=50,
        )

    async def test_list_issues_filter_by_type(
        self, issues_client, auth_headers, mock_issue_service
    ):
        resp = await issues_client.get("/api/issues?type=bug", headers=auth_headers)
        assert resp.status_code == 200
        mock_issue_service.list_issues.assert_called_once_with(
            state="open",
            repo_full_name=None,
            for_entity=None,
            from_entity=None,
            issue_type="bug",
            label=None,
            page=1,
            per_page=50,
        )

    async def test_list_issues_filter_by_state(
        self, issues_client, auth_headers, mock_issue_service
    ):
        resp = await issues_client.get("/api/issues?state=closed", headers=auth_headers)
        assert resp.status_code == 200
        mock_issue_service.list_issues.assert_called_once_with(
            state="closed",
            repo_full_name=None,
            for_entity=None,
            from_entity=None,
            issue_type=None,
            label=None,
            page=1,
            per_page=50,
        )

    async def test_list_issues_requires_auth(self, issues_client):
        resp = await issues_client.get("/api/issues")
        assert resp.status_code == 401


class TestGetIssue:
    async def test_get_issue_by_number(
        self, issues_client, auth_headers, mock_issue_service
    ):
        resp = await issues_client.get(
            f"/api/issues/{_OWNER}/{_REPO}/42", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["number"] == 42
        assert data["title"] == "[task] Update config"
        assert data["html_url"] == f"https://github.com/{_OWNER}/{_REPO}/issues/42"
        assert "priority_score" in data
        mock_issue_service.get_issue.assert_called_once_with(
            f"{_OWNER}/{_REPO}", 42
        )

    async def test_get_issue_not_found(
        self, issues_client, auth_headers, mock_issue_service
    ):
        mock_issue_service.get_issue.return_value = None
        resp = await issues_client.get(
            f"/api/issues/{_OWNER}/{_REPO}/999", headers=auth_headers
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    async def test_get_issue_upstream_failure_raises(
        self, issues_client, auth_headers, mock_issue_service
    ):
        mock_issue_service.get_issue.side_effect = RuntimeError("DB connection failed")
        with pytest.raises(RuntimeError, match="DB connection failed"):
            await issues_client.get(
                f"/api/issues/{_OWNER}/{_REPO}/42", headers=auth_headers
            )


class TestListComments:
    async def test_list_comments_parses_from_tag(
        self, issues_client, auth_headers, mock_issue_service
    ):
        resp = await issues_client.get(
            f"/api/issues/{_OWNER}/{_REPO}/42/comments", headers=auth_headers
        )
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
        mock_issue_service.list_comments.assert_called_once_with(
            f"{_OWNER}/{_REPO}", 42
        )


class TestGetDependencies:
    async def test_dependencies_empty(
        self, issues_client, auth_headers, mock_issue_service, mock_github
    ):
        resp = await issues_client.get(
            f"/api/issues/{_OWNER}/{_REPO}/42/dependencies", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sub_issues"] == []
        assert data["parents"] == []
        mock_github.get_sub_issues.assert_called_once_with(
            42, repo_full_name=f"{_OWNER}/{_REPO}"
        )

    async def test_dependencies_with_sub_issues(
        self, issues_client, auth_headers, mock_issue_service, mock_github
    ):
        from agent_backbone.models import IssueData, ParsedLabels

        sub = IssueData(
            number=50,
            title="[task] Sub-task",
            state="open",
            labels=ParsedLabels(
                sender="ike",
                targets=["ada"],
                issue_type="task",
                all_labels=["from:ike", "for:ada", "task"],
            ),
            html_url=f"https://github.com/{_OWNER}/{_REPO}/issues/50",
            repo_full_name=f"{_OWNER}/{_REPO}",
        )
        mock_github.get_sub_issues.return_value = [sub]
        # When the route looks up the sub-issue in the projection, return a detail dict
        mock_issue_service.get_issue.side_effect = [
            _SAMPLE_DETAIL_1,  # first call checks the parent exists
            {  # second call looks up the sub-issue
                "repo_full_name": f"{_OWNER}/{_REPO}",
                "number": 50,
                "title": "[task] Sub-task",
                "state": "open",
                "html_url": f"https://github.com/{_OWNER}/{_REPO}/issues/50",
                "labels": {
                    "sender": "ike",
                    "targets": ["ada"],
                    "issue_type": "task",
                    "priority": "",
                    "all_labels": ["from:ike", "for:ada", "task"],
                },
                "priority_score": 5.0,
                "created_at": "",
                "updated_at": "",
                "comment_count": 0,
                "user_login": "eandualem",
                "body": "",
            },
        ]
        resp = await issues_client.get(
            f"/api/issues/{_OWNER}/{_REPO}/42/dependencies", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sub_issues"]) == 1
        assert data["sub_issues"][0]["number"] == 50


class TestCreateIssue:
    async def test_create_issue_success(
        self, issues_client, auth_headers, mock_issue_service
    ):
        payload = {
            "title": "[task] New issue",
            "body": "## Context\nTest",
            "labels": ["from:leo", "for:ike", "task"],
            "repo_full_name": f"{_OWNER}/{_REPO}",
        }
        resp = await issues_client.post("/api/issues", json=payload, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["number"] == 99
        assert data["title"] == "[task] New issue"
        mock_issue_service.create_issue.assert_called_once_with(
            repo_full_name=f"{_OWNER}/{_REPO}",
            title="[task] New issue",
            body="## Context\nTest",
            labels=["from:leo", "for:ike", "task"],
        )

    async def test_create_issue_missing_title(self, issues_client, auth_headers):
        payload = {"body": "no title", "repo_full_name": f"{_OWNER}/{_REPO}"}
        resp = await issues_client.post("/api/issues", json=payload, headers=auth_headers)
        assert resp.status_code == 400
        assert "title" in resp.json()["detail"].lower()

    async def test_create_issue_empty_title(self, issues_client, auth_headers):
        payload = {"title": "", "body": "empty title", "repo_full_name": f"{_OWNER}/{_REPO}"}
        resp = await issues_client.post("/api/issues", json=payload, headers=auth_headers)
        assert resp.status_code == 400


class TestAddComment:
    async def test_add_comment_success(
        self, issues_client, auth_headers, mock_issue_service
    ):
        payload = {"body": "[from:ike] Acknowledged"}
        resp = await issues_client.post(
            f"/api/issues/{_OWNER}/{_REPO}/42/comment",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 10
        assert data["body"] == "[from:ike] Acknowledged"
        assert data["from_entity"] == "ike"
        mock_issue_service.add_comment.assert_called_once_with(
            repo_full_name=f"{_OWNER}/{_REPO}",
            issue_number=42,
            body="[from:ike] Acknowledged",
        )

    async def test_add_comment_missing_body(self, issues_client, auth_headers):
        payload = {"something": "else"}
        resp = await issues_client.post(
            f"/api/issues/{_OWNER}/{_REPO}/42/comment",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "body" in resp.json()["detail"].lower()

    async def test_add_comment_empty_body(self, issues_client, auth_headers):
        payload = {"body": ""}
        resp = await issues_client.post(
            f"/api/issues/{_OWNER}/{_REPO}/42/comment",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_add_comment_rate_limited_returns_retry_after(
        self, issues_client, auth_headers, mock_issue_service
    ):
        mock_issue_service.add_comment.side_effect = GitHubServiceError(
            "GitHub API 403 for POST "
            "/repos/eandualem/orchestration/issues/42/comments: secondary rate limit",
            status_code=403,
            retry_after_seconds=2.4,
            retry_allowed=True,
        )
        payload = {"body": "[from:ike] Acknowledged"}
        resp = await issues_client.post(
            f"/api/issues/{_OWNER}/{_REPO}/42/comment",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "3"


class TestUpdateIssue:
    async def test_update_issue_close(
        self, issues_client, auth_headers, mock_issue_service
    ):
        payload = {"state": "closed"}
        resp = await issues_client.patch(
            f"/api/issues/{_OWNER}/{_REPO}/42",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["number"] == 42
        assert data["state"] == "closed"
        mock_issue_service.update_issue_state.assert_called_once_with(
            repo_full_name=f"{_OWNER}/{_REPO}",
            issue_number=42,
            state="closed",
        )

    async def test_update_issue_missing_state(self, issues_client, auth_headers):
        payload = {"title": "new title"}
        resp = await issues_client.patch(
            f"/api/issues/{_OWNER}/{_REPO}/42",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "state" in resp.json()["detail"].lower()
