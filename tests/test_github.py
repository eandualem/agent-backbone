"""Tests for src/github.py."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.config import BackboneConfig
from src.github import API_BASE, GitHubClient


@pytest.fixture
def config():
    return BackboneConfig(
        github_token="test-token",
        github_owner="eandualem",
        github_repo="orchestration",
    )


@pytest.fixture
def issues_url():
    return f"{API_BASE}/repos/eandualem/orchestration/issues"


class TestListOpenIssues:
    @respx.mock
    async def test_returns_sorted_issues(self, config, issues_url):
        respx.get(issues_url).respond(
            json=[
                {
                    "number": 5,
                    "title": "[task] Older issue",
                    "state": "open",
                    "labels": [{"name": "for:ike"}, {"name": "from:leo"}, {"name": "task"}],
                },
                {
                    "number": 8,
                    "title": "[bug] Blocking bug",
                    "state": "open",
                    "labels": [
                        {"name": "for:ike"},
                        {"name": "from:ada"},
                        {"name": "bug"},
                        {"name": "blocking"},
                    ],
                },
            ]
        )

        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")

        # Blocking issue should come first
        assert len(issues) == 2
        assert issues[0].number == 8
        assert issues[0].labels.priority == "blocking"
        assert issues[1].number == 5

    @respx.mock
    async def test_empty_response(self, config, issues_url):
        respx.get(issues_url).respond(json=[])

        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")

        assert issues == []

    @respx.mock
    async def test_filters_pull_requests(self, config, issues_url):
        respx.get(issues_url).respond(
            json=[
                {
                    "number": 1,
                    "title": "Real issue",
                    "state": "open",
                    "labels": [{"name": "for:ike"}],
                },
                {
                    "number": 2,
                    "title": "A PR",
                    "state": "open",
                    "labels": [{"name": "for:ike"}],
                    "pull_request": {"url": "..."},
                },
            ]
        )

        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")

        assert len(issues) == 1
        assert issues[0].number == 1

    @respx.mock
    async def test_api_error(self, config, issues_url):
        respx.get(issues_url).respond(status_code=500)

        async with GitHubClient(config) as gh:
            with pytest.raises(httpx.HTTPStatusError):
                await gh.list_open_issues("for:ike")


class TestGetIssue:
    @respx.mock
    async def test_get_issue(self, config):
        url = f"{API_BASE}/repos/eandualem/orchestration/issues/42"
        respx.get(url).respond(
            json={
                "number": 42,
                "title": "[task] Update config",
                "state": "open",
                "labels": [{"name": "from:leo"}, {"name": "for:ike"}, {"name": "task"}],
            }
        )

        async with GitHubClient(config) as gh:
            issue = await gh.get_issue(42)

        assert issue.number == 42
        assert issue.labels.sender == "leo"
        assert issue.labels.issue_type == "task"
