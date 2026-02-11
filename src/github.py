"""GitHub REST API client for querying issues.

Uses httpx for async HTTP. Queries open issues by label for
close-then-next and agent_monitor flows.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.config import BackboneConfig
from src.models import IssueData, ParsedLabels

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
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {self._config.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

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
            # Skip pull requests (GitHub API returns them mixed with issues)
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

        # Sort: blocking issues first, then by issue number (oldest first)
        issues.sort(key=lambda i: (0 if i.labels.priority == "blocking" else 1, i.number))
        return issues

    async def get_issue(self, issue_number: int) -> IssueData:
        """Get a single issue by number."""
        assert self._client is not None
        url = (
            f"/repos/{self._config.github_owner}/{self._config.github_repo}"
            f"/issues/{issue_number}"
        )
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
