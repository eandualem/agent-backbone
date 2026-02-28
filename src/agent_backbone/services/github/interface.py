"""GitHub REST API client for querying issues.

Uses httpx for async HTTP. Queries open issues by label for
close-then-next and agent_monitor flows.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agent_backbone.config import BackboneConfig
from agent_backbone.models import CommentData, IssueData, ParsedLabels
from agent_backbone.services.delivery import compute_priority_score

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"


class GitHubClient:
    """Async GitHub REST API client.

    Usage:
        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")
    """

    def __init__(self, config: BackboneConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GitHubClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # --- LifecycleAware methods ---

    async def start(self) -> None:
        """Initialize the HTTP client (LifecycleAware)."""
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {self._config.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def stop(self) -> None:
        """Close the HTTP client (LifecycleAware)."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> dict:
        """Check GitHub API connectivity."""
        return {
            "healthy": self._client is not None,
            "service": "github",
            "client_active": self._client is not None,
        }

    # --- Issue operations ---

    async def list_open_issues(self, label: str) -> list[IssueData]:
        """List open issues with a specific label, sorted for delivery.

        Returns issues sorted: blocking first, then oldest first (FIFO).
        """
        assert self._client is not None
        url = f"/repos/{self._config.github_owner}/{self._config.github_repo}/issues"
        resp = await self._client.get(
            url,
            params={
                "state": "open",
                "labels": label,
                "sort": "created",
                "direction": "asc",
                "per_page": 50,
            },
        )
        resp.raise_for_status()

        issues: list[IssueData] = []
        for item in resp.json():
            if "pull_request" in item:
                continue
            labels = ParsedLabels.from_github_labels(item.get("labels", []))
            issues.append(
                IssueData(
                    number=item["number"],
                    title=item.get("title", ""),
                    state=item.get("state", "open"),
                    labels=labels,
                    html_url=item.get("html_url", ""),
                )
            )

        scoring_config = self._config.priority_scoring
        issues.sort(
            key=lambda i: (
                -compute_priority_score(i, scoring_config),
                i.number,
            )
        )
        return issues

    async def get_sub_issues(self, issue_number: int) -> list[IssueData]:
        """Fetch sub-issues of a parent issue.

        Returns empty list on error (404, timeout, rate limit).
        """
        assert self._client is not None
        url = (
            f"/repos/{self._config.github_owner}/{self._config.github_repo}"
            f"/issues/{issue_number}/sub_issues"
        )
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.warning("Failed to fetch sub-issues for #%d: %s", issue_number, e)
            return []
        except httpx.TimeoutException:
            log.warning("Timeout fetching sub-issues for #%d", issue_number)
            return []

        results: list[IssueData] = []
        for item in resp.json():
            labels = ParsedLabels.from_github_labels(item.get("labels", []))
            results.append(
                IssueData(
                    number=item["number"],
                    title=item.get("title", ""),
                    state=item.get("state", "open"),
                    labels=labels,
                    html_url=item.get("html_url", ""),
                )
            )
        return results

    async def count_open_sub_issues(self, issue_number: int) -> int:
        """Count open sub-issues of a parent. Returns 0 on error."""
        subs = await self.get_sub_issues(issue_number)
        return sum(1 for s in subs if s.state == "open")

    async def get_issue(self, issue_number: int) -> IssueData:
        """Get a single issue by number."""
        assert self._client is not None
        url = f"/repos/{self._config.github_owner}/{self._config.github_repo}/issues/{issue_number}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        item = resp.json()
        labels = ParsedLabels.from_github_labels(item.get("labels", []))
        return IssueData(
            number=item["number"],
            title=item.get("title", ""),
            state=item.get("state", "open"),
            labels=labels,
            html_url=item.get("html_url", ""),
        )

    async def list_issues(
        self,
        state: str = "open",
        labels: list[str] | None = None,
        per_page: int = 50,
    ) -> list[IssueData]:
        """List issues with flexible filtering by state and labels.

        Unlike list_open_issues, supports state=open/closed/all and multiple labels.
        """
        assert self._client is not None
        url = f"/repos/{self._config.github_owner}/{self._config.github_repo}/issues"
        params: dict[str, str | int] = {
            "state": state,
            "sort": "created",
            "direction": "asc",
            "per_page": per_page,
        }
        if labels:
            params["labels"] = ",".join(labels)

        resp = await self._client.get(url, params=params)
        resp.raise_for_status()

        issues: list[IssueData] = []
        for item in resp.json():
            if "pull_request" in item:
                continue
            parsed = ParsedLabels.from_github_labels(item.get("labels", []))
            issues.append(
                IssueData(
                    number=item["number"],
                    title=item.get("title", ""),
                    state=item.get("state", "open"),
                    labels=parsed,
                    html_url=item.get("html_url", ""),
                )
            )

        scoring_config = self._config.priority_scoring
        issues.sort(
            key=lambda i: (
                -compute_priority_score(i, scoring_config),
                i.number,
            )
        )
        return issues

    async def list_comments(self, issue_number: int) -> list[CommentData]:
        """List comments on an issue."""
        assert self._client is not None
        url = (
            f"/repos/{self._config.github_owner}/{self._config.github_repo}"
            f"/issues/{issue_number}/comments"
        )
        resp = await self._client.get(url, params={"per_page": 100})
        resp.raise_for_status()

        return [
            CommentData(
                id=item.get("id", 0),
                body=item.get("body", ""),
                user_login=item.get("user", {}).get("login", "unknown"),
            )
            for item in resp.json()
        ]

    async def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> IssueData:
        """Create a new issue in the orchestration repo."""
        assert self._client is not None
        url = f"/repos/{self._config.github_owner}/{self._config.github_repo}/issues"
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels

        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        item = resp.json()
        parsed = ParsedLabels.from_github_labels(item.get("labels", []))
        return IssueData(
            number=item["number"],
            title=item.get("title", ""),
            state=item.get("state", "open"),
            labels=parsed,
            html_url=item.get("html_url", ""),
        )

    async def add_comment(self, issue_number: int, body: str) -> CommentData:
        """Add a comment to an issue."""
        assert self._client is not None
        url = (
            f"/repos/{self._config.github_owner}/{self._config.github_repo}"
            f"/issues/{issue_number}/comments"
        )
        resp = await self._client.post(url, json={"body": body})
        resp.raise_for_status()
        item = resp.json()
        return CommentData(
            id=item.get("id", 0),
            body=item.get("body", ""),
            user_login=item.get("user", {}).get("login", "unknown"),
        )

    async def update_issue(self, issue_number: int, state: str) -> IssueData:
        """Update an issue's state (open/closed)."""
        assert self._client is not None
        url = f"/repos/{self._config.github_owner}/{self._config.github_repo}/issues/{issue_number}"
        resp = await self._client.patch(url, json={"state": state})
        resp.raise_for_status()
        item = resp.json()
        parsed = ParsedLabels.from_github_labels(item.get("labels", []))
        return IssueData(
            number=item["number"],
            title=item.get("title", ""),
            state=item.get("state", "open"),
            labels=parsed,
            html_url=item.get("html_url", ""),
        )
