"""Tests for agent_backbone/services/github."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from agent_backbone.config import BackboneConfig, GitHubConfig
from agent_backbone.services.github import API_BASE, GitHubClient, GitHubServiceError

_TEST_GITHUB_APP_KEY = Path(__file__).parents[3] / "fixtures" / "github-app-test-key.pem"
_ORIGINAL_GET_INSTALLATION_TOKEN = GitHubClient._get_installation_token


@pytest.fixture
def config():
    return BackboneConfig(
        github_app_id=3075015,
        github_app_private_key_path=str(_TEST_GITHUB_APP_KEY),
        github=GitHubConfig(owner="eandualem", repo="orchestration"),
    )


@pytest.fixture
def issues_url():
    return f"{API_BASE}/repos/eandualem/orchestration/issues"


@pytest.fixture(autouse=True)
def mock_installation_token(monkeypatch):
    async def _fake_installation_token(self, repo_full_name=None):
        return "installation-token"

    monkeypatch.setattr(GitHubClient, "_get_installation_token", _fake_installation_token)


class TestListOpenIssues:
    async def test_start_requires_github_app_credentials(self):
        config = BackboneConfig(github_app_id=None, github_app_private_key_path="")

        with pytest.raises(RuntimeError, match="GITHUB_APP_ID"):
            async with GitHubClient(config):
                pass

    @respx.mock
    async def test_fetches_installation_token_via_github_app(self, config, monkeypatch):
        monkeypatch.setattr(
            GitHubClient,
            "_get_installation_token",
            _ORIGINAL_GET_INSTALLATION_TOKEN,
        )
        monkeypatch.setattr(GitHubClient, "_build_app_jwt", lambda self: "app-jwt")

        installation_url = f"{API_BASE}/repos/eandualem/orchestration/installation"
        token_url = f"{API_BASE}/app/installations/321/access_tokens"
        issues_url = f"{API_BASE}/repos/eandualem/orchestration/issues"
        install_route = respx.get(installation_url).respond(json={"id": 321})
        token_route = respx.post(token_url).respond(
            status_code=201,
            json={"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"},
        )
        issues_route = respx.get(issues_url).respond(json=[])

        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")

        assert issues == []
        assert install_route.called
        assert token_route.called
        assert issues_route.called

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

        # Sorted by number ascending (delivery order)
        assert len(issues) == 2
        assert issues[0].number == 5
        assert issues[1].number == 8
        assert issues[1].labels.priority == "blocking"

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
            with pytest.raises(GitHubServiceError):
                await gh.list_open_issues("for:ike")

    @respx.mock
    async def test_uses_requested_repo_for_multi_repo_queries(self, config):
        url = f"{API_BASE}/repos/WF/agent-shell/issues"
        respx.get(url).respond(
            json=[
                {
                    "number": 5,
                    "title": "[task] Cross-repo issue",
                    "state": "open",
                    "labels": [{"name": "for:ike"}, {"name": "from:leo"}, {"name": "task"}],
                    "html_url": "https://github.com/WF/agent-shell/issues/5",
                }
            ]
        )

        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike", repo_full_name="WF/agent-shell")

        assert len(issues) == 1
        assert issues[0].repo_full_name == "WF/agent-shell"

    @respx.mock
    async def test_refreshes_installation_token_after_401(self, config, monkeypatch, issues_url):
        monkeypatch.setattr(
            GitHubClient,
            "_get_installation_token",
            _ORIGINAL_GET_INSTALLATION_TOKEN,
        )
        monkeypatch.setattr(GitHubClient, "_build_app_jwt", lambda self: "app-jwt")

        installation_url = f"{API_BASE}/repos/eandualem/orchestration/installation"
        token_url = f"{API_BASE}/app/installations/321/access_tokens"
        respx.get(installation_url).respond(json={"id": 321})
        token_route = respx.post(token_url).mock(
            side_effect=[
                httpx.Response(
                    201,
                    json={"token": "stale-token", "expires_at": "2099-01-01T00:00:00Z"},
                ),
                httpx.Response(
                    201,
                    json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
                ),
            ]
        )
        issues_route = respx.get(issues_url).mock(
            side_effect=[
                httpx.Response(401, json={"message": "Bad credentials"}),
                httpx.Response(200, json=[]),
            ]
        )

        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")

        assert issues == []
        assert issues_route.call_count == 2
        assert token_route.call_count == 2

    @respx.mock
    async def test_retries_safe_get_after_server_error(self, config, issues_url, monkeypatch):
        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        route = respx.get(issues_url).mock(
            side_effect=[
                httpx.Response(502, json={"message": "Bad gateway"}),
                httpx.Response(200, json=[]),
            ]
        )

        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")

        assert issues == []
        assert route.call_count == 2
        assert sleep_calls == [0.5]

    @respx.mock
    async def test_retries_rate_limited_request_when_retry_after_is_short(
        self, config, issues_url, monkeypatch
    ):
        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        route = respx.get(issues_url).mock(
            side_effect=[
                httpx.Response(
                    403,
                    headers={"Retry-After": "1"},
                    json={"message": "You have exceeded a secondary rate limit"},
                ),
                httpx.Response(200, json=[]),
            ]
        )

        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")

        assert issues == []
        assert route.call_count == 2
        assert sleep_calls == [1.0]


class TestMutatingRequests:
    async def test_serializes_and_spaces_mutations(self, config, monkeypatch):
        call_times = iter([10.0, 10.0, 10.2, 11.3])
        last_time = 11.3
        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        def _fake_monotonic() -> float:
            nonlocal last_time
            try:
                last_time = next(call_times)
            except StopIteration:
                pass
            return last_time

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        monkeypatch.setattr(
            "agent_backbone.services.github.interface.time.monotonic",
            _fake_monotonic,
        )

        async with GitHubClient(config) as gh:
            request = AsyncMock(
                side_effect=[
                    httpx.Response(
                        201,
                        json={"id": 1, "body": "ok", "user": {"login": "eandualem"}},
                    ),
                    httpx.Response(
                        201,
                        json={"id": 2, "body": "ok", "user": {"login": "eandualem"}},
                    ),
                ]
            )
            monkeypatch.setattr(gh._client, "request", request)
            await gh.add_comment(42, "[from:ike] first")
            await gh.add_comment(42, "[from:ike] second")

        assert sleep_calls == [pytest.approx(0.8)]


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


class TestGetSubIssues:
    @respx.mock
    async def test_returns_sub_issues(self, config):
        url = f"{API_BASE}/repos/eandualem/orchestration/issues/10/sub_issues"
        respx.get(url).respond(
            json=[
                {
                    "number": 20,
                    "title": "[task] Sub task 1",
                    "state": "closed",
                    "labels": [{"name": "for:feynman"}, {"name": "task"}],
                },
                {
                    "number": 21,
                    "title": "[task] Sub task 2",
                    "state": "open",
                    "labels": [{"name": "for:feynman"}, {"name": "task"}],
                },
            ]
        )

        async with GitHubClient(config) as gh:
            subs = await gh.get_sub_issues(10)

        assert len(subs) == 2
        assert subs[0].number == 20
        assert subs[0].state == "closed"
        assert subs[1].number == 21
        assert subs[1].state == "open"

    @respx.mock
    async def test_404_returns_empty(self, config):
        url = f"{API_BASE}/repos/eandualem/orchestration/issues/99/sub_issues"
        respx.get(url).respond(status_code=404)

        async with GitHubClient(config) as gh:
            subs = await gh.get_sub_issues(99)

        assert subs == []

    @respx.mock
    async def test_empty_sub_issues(self, config):
        url = f"{API_BASE}/repos/eandualem/orchestration/issues/10/sub_issues"
        respx.get(url).respond(json=[])

        async with GitHubClient(config) as gh:
            subs = await gh.get_sub_issues(10)

        assert subs == []


class TestCountOpenSubIssues:
    @respx.mock
    async def test_counts_open_only(self, config):
        url = f"{API_BASE}/repos/eandualem/orchestration/issues/10/sub_issues"
        respx.get(url).respond(
            json=[
                {"number": 20, "title": "Sub 1", "state": "closed", "labels": []},
                {"number": 21, "title": "Sub 2", "state": "open", "labels": []},
                {"number": 22, "title": "Sub 3", "state": "open", "labels": []},
            ]
        )

        async with GitHubClient(config) as gh:
            count = await gh.count_open_sub_issues(10)

        assert count == 2

    @respx.mock
    async def test_error_returns_zero(self, config):
        url = f"{API_BASE}/repos/eandualem/orchestration/issues/99/sub_issues"
        respx.get(url).respond(status_code=500)

        async with GitHubClient(config) as gh:
            count = await gh.count_open_sub_issues(99)

        assert count == 0
