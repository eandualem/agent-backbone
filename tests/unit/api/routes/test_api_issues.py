"""Tests for the issue endpoints at api/routes/issues.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.api.deps import get_github
from agent_backbone.models import CommentData, IssueData, ParsedLabels
from tests.conftest import TEST_REPO


def _issue(
    number: int, targets: list[str] | None = None, priority: str = "", **kwargs
) -> IssueData:
    return IssueData(
        number=number,
        title=f"[task] Issue {number}",
        labels=ParsedLabels(
            sender="leo", targets=targets or ["ike"], issue_type="task", priority=priority
        ),
        html_url=f"https://github.com/{TEST_REPO}/issues/{number}",
        repo_full_name=TEST_REPO,
        **kwargs,
    )


@pytest.fixture
def gh(api_app):
    client = AsyncMock()
    client.list_issues = AsyncMock(return_value=[_issue(1), _issue(2, priority="blocking")])
    client.get_issue = AsyncMock(return_value=_issue(1))
    client.list_comments = AsyncMock(
        return_value=[CommentData(id=1, body="[from:ike] ack", user_login="bot")]
    )
    client.get_sub_issues = AsyncMock(return_value=[_issue(3)])
    client.create_issue = AsyncMock(return_value=_issue(10, ["feynman"]))
    client.add_comment = AsyncMock(return_value=CommentData(id=5, body="hi", user_login="me"))
    client.update_issue = AsyncMock(return_value=_issue(1, state="closed"))
    api_app.dependency_overrides[get_github] = lambda: client
    yield client
    api_app.dependency_overrides.pop(get_github, None)


class TestListIssues:
    async def test_lists_with_priority_scores(self, api_client, auth_headers, gh):
        resp = await api_client.get(
            f"/api/issues?for=ike&type=task&repo={TEST_REPO}", headers=auth_headers
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        # Highest priority first: the blocking issue leads.
        assert [i["number"] for i in data["items"]] == [2, 1]
        assert data["items"][0]["priority_score"] > data["items"][1]["priority_score"]
        assert gh.list_issues.await_args.kwargs["labels"] == ["for:ike", "task"]
        assert gh.list_issues.await_args.kwargs["repo_full_name"] == TEST_REPO

    async def test_repo_is_required(self, api_client, auth_headers, gh):
        resp = await api_client.get("/api/issues", headers=auth_headers)
        assert resp.status_code == 422

    async def test_without_github_returns_503(self, api_client, auth_headers):
        resp = await api_client.get(f"/api/issues?repo={TEST_REPO}", headers=auth_headers)
        assert resp.status_code == 503


class TestGetIssue:
    async def test_returns_issue(self, api_client, auth_headers, gh):
        resp = await api_client.get(f"/api/issues/1?repo={TEST_REPO}", headers=auth_headers)
        assert resp.json()["number"] == 1
        assert resp.json()["labels"]["targets"] == ["ike"]

    async def test_not_found(self, api_client, auth_headers, gh):
        gh.get_issue.side_effect = RuntimeError("404")
        resp = await api_client.get(f"/api/issues/999?repo={TEST_REPO}", headers=auth_headers)
        assert resp.status_code == 404


class TestComments:
    async def test_lists_comments_with_from_tag(self, api_client, auth_headers, gh):
        resp = await api_client.get(
            f"/api/issues/1/comments?repo={TEST_REPO}", headers=auth_headers
        )
        assert resp.json()["items"][0]["from_entity"] == "ike"

    async def test_adds_comment(self, api_client, auth_headers, gh):
        resp = await api_client.post(
            f"/api/issues/1/comment?repo={TEST_REPO}", json={"body": "hi"}, headers=auth_headers
        )
        assert resp.json()["id"] == 5
        gh.add_comment.assert_awaited_once_with(1, "hi", repo_full_name=TEST_REPO)

    async def test_empty_comment_rejected(self, api_client, auth_headers, gh):
        resp = await api_client.post(
            f"/api/issues/1/comment?repo={TEST_REPO}", json={"body": ""}, headers=auth_headers
        )
        assert resp.status_code == 400


class TestDependencies:
    async def test_returns_sub_issues_and_parents(self, api_client, auth_headers, gh, api_app):
        await api_app.state.db.sync_dependencies(7, [1], repo=TEST_REPO)
        resp = await api_client.get(
            f"/api/issues/1/dependencies?repo={TEST_REPO}", headers=auth_headers
        )
        assert [s["number"] for s in resp.json()["sub_issues"]] == [3]
        assert resp.json()["parents"] == [7]


class TestCreateIssue:
    async def test_creates_and_notifies(self, api_client, auth_headers, gh, api_app):
        create = AsyncMock(return_value=_issue(10, ["feynman"]))
        with patch("agent_backbone.api.routes.issues.create_and_notify", create):
            resp = await api_client.post(
                "/api/issues",
                json={
                    "title": "[task] New",
                    "body": "b",
                    "labels": ["for:feynman", "task"],
                    "repo": TEST_REPO,
                },
                headers=auth_headers,
            )

        assert resp.status_code == 201
        assert resp.json()["number"] == 10
        create.assert_awaited_once()
        assert create.await_args.kwargs["repo"] == TEST_REPO

    async def test_rejects_unknown_target(self, api_client, auth_headers, gh):
        resp = await api_client.post(
            "/api/issues",
            json={"title": "x", "labels": ["for:nobody"], "repo": TEST_REPO},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "unknown issue target" in resp.json()["detail"]

    async def test_repo_is_required(self, api_client, auth_headers, gh):
        resp = await api_client.post(
            "/api/issues", json={"title": "x", "labels": []}, headers=auth_headers
        )
        assert resp.status_code == 422


class TestUpdateIssue:
    async def test_closes_issue(self, api_client, auth_headers, gh):
        resp = await api_client.patch(
            f"/api/issues/1?repo={TEST_REPO}", json={"state": "closed"}, headers=auth_headers
        )
        assert resp.json()["state"] == "closed"
        gh.update_issue.assert_awaited_once_with(1, "closed", repo_full_name=TEST_REPO)

    async def test_invalid_state(self, api_client, auth_headers, gh):
        resp = await api_client.patch(
            f"/api/issues/1?repo={TEST_REPO}", json={"state": "meh"}, headers=auth_headers
        )
        assert resp.status_code == 400
