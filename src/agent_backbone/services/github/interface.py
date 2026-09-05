"""GitHub REST API client for issue operations.

Two authentication modes:

* **Token** — ``GITHUB_TOKEN`` (a PAT or ``gh auth token``). Simplest; use it.
* **GitHub App** — ``GITHUB_APP_ID`` + ``GITHUB_APP_PRIVATE_KEY_PATH``; requests
  are authenticated with per-repository installation tokens. Needed when the
  backbone should act as its own identity rather than as you.

When both are present the token wins.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from agent_backbone.models import CommentData, IssueData, ParsedLabels

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
_TOKEN_REFRESH_SKEW = timedelta(minutes=1)


@dataclass(slots=True)
class _CachedInstallationToken:
    token: str
    expires_at: datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_github_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(UTC)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GitHubClient:
    """Async GitHub REST API client.

    Every call names its repository: there is no default repository.

    Results come back in GitHub's order (oldest first); routing sorts an
    agent's queue by priority.

    Usage:
        async with GitHubClient(config) as gh:
            issues = await gh.list_issues(labels=["for:reviewer"], repo_full_name="me/app")
    """

    def __init__(self, config: BackboneConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._private_key: Any | None = None
        self._installation_ids: dict[str, int] = {}
        self._installation_tokens: dict[str, _CachedInstallationToken] = {}

    async def __aenter__(self) -> GitHubClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # --- LifecycleAware methods ---

    @property
    def auth_mode(self) -> str:
        if self._config.github_token:
            return "token"
        if self._config.github_app_ready:
            return "github_app"
        return "none"

    async def start(self) -> None:
        """Initialize the HTTP client (LifecycleAware)."""
        if self.auth_mode == "none":
            raise RuntimeError(
                "GitHub auth not configured: set GITHUB_TOKEN, or GITHUB_APP_ID + "
                "GITHUB_APP_PRIVATE_KEY_PATH"
            )
        if self.auth_mode == "github_app":
            self._validate_app_config()
            self._load_private_key()
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
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
        self._installation_ids.clear()
        self._installation_tokens.clear()

    async def health_check(self) -> dict:
        """Check GitHub API connectivity."""
        return {
            "healthy": self._client is not None,
            "service": "github",
            "client_active": self._client is not None,
            "auth_mode": self.auth_mode,
        }

    # --- Repo resolution ---

    @staticmethod
    def _resolve_repo(repo_full_name: str | None) -> tuple[str, str]:
        candidate = repo_full_name or ""
        if candidate and "/" in candidate:
            owner, repo = candidate.split("/", 1)
            if owner and repo:
                return owner, repo
        raise ValueError("A repository (owner/name) is required for GitHub operations")

    # --- GitHub App auth ---

    def _validate_app_config(self) -> None:
        key_path = Path(self._config.github_app_private_key_path).expanduser()
        if not key_path.is_file():
            raise RuntimeError(f"GitHub App private key not found: {key_path}")

    def _load_private_key(self) -> Any:
        if self._private_key is not None:
            return self._private_key

        try:
            from cryptography.hazmat.primitives import serialization
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "GitHub App auth needs the 'github-app' extra: "
                "uv tool install 'agent-backbone[github-app]' "
                "(or pip install 'agent-backbone[github-app]')"
            ) from exc

        key_path = Path(self._config.github_app_private_key_path).expanduser()
        try:
            self._private_key = serialization.load_pem_private_key(
                key_path.read_bytes(),
                password=None,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Failed to load GitHub App private key: {key_path}") from exc
        return self._private_key

    def _build_app_jwt(self) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key = self._load_private_key()
        header = {"alg": "RS256", "typ": "JWT"}
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 600,
            "iss": self._config.github_app_id,
        }
        segments = [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
        ]
        signing_input = ".".join(segments).encode()
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return ".".join([*segments, _b64url(signature)])

    async def _app_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._build_app_jwt()}"
        return await self._client.request(method, url, headers=headers, **kwargs)

    async def _get_installation_id(self, owner: str, repo: str) -> int:
        repo_key = f"{owner}/{repo}"
        cached = self._installation_ids.get(repo_key)
        if cached is not None:
            return cached

        resp = await self._app_request("GET", f"/repos/{owner}/{repo}/installation")
        resp.raise_for_status()
        installation_id = int(resp.json()["id"])
        self._installation_ids[repo_key] = installation_id
        return installation_id

    async def _get_installation_token(self, owner: str, repo: str) -> str:
        repo_key = f"{owner}/{repo}"
        cached = self._installation_tokens.get(repo_key)
        if cached and cached.expires_at > (_utcnow() + _TOKEN_REFRESH_SKEW):
            return cached.token

        installation_id = await self._get_installation_id(owner, repo)
        resp = await self._app_request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
        )
        resp.raise_for_status()

        payload = resp.json()
        token = payload.get("token", "")
        expires_at_raw = payload.get("expires_at", "")
        if not isinstance(token, str) or not token:
            raise RuntimeError("GitHub App installation token response missing token")
        if not isinstance(expires_at_raw, str) or not expires_at_raw:
            raise RuntimeError("GitHub App installation token response missing expires_at")

        self._installation_tokens[repo_key] = _CachedInstallationToken(
            token=token,
            expires_at=_parse_github_timestamp(expires_at_raw),
        )
        return token

    async def _auth_header(self, owner: str, repo: str) -> str:
        if self._config.github_token:
            return f"Bearer {self._config.github_token}"
        return f"Bearer {await self._get_installation_token(owner, repo)}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        repo_full_name: str | None,
        **kwargs: Any,
    ) -> httpx.Response:
        """``path`` is relative to ``/repos/{owner}/{repo}``; the repository is parsed once."""
        assert self._client is not None
        owner, repo = self._resolve_repo(repo_full_name)
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = await self._auth_header(owner, repo)
        resp = await self._client.request(
            method, f"/repos/{owner}/{repo}{path}", headers=headers, **kwargs
        )
        resp.raise_for_status()
        return resp

    async def _request_all(
        self, path: str, *, repo_full_name: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Every item of a paginated listing: GitHub caps a page at 100 and the
        rest is only reachable through the ``Link: rel="next"`` header."""
        assert self._client is not None
        resp = await self._request("GET", path, repo_full_name=repo_full_name, params=params)
        items: list[dict[str, Any]] = list(resp.json())
        while (nxt := resp.links.get("next")) and nxt.get("url"):
            owner, repo = self._resolve_repo(repo_full_name)
            headers = {"Authorization": await self._auth_header(owner, repo)}
            resp = await self._client.get(nxt["url"], headers=headers)
            resp.raise_for_status()
            items.extend(resp.json())
        return items

    @staticmethod
    def _build_issue(item: dict[str, Any], repo_full_name: str) -> IssueData:
        labels = ParsedLabels.from_github_labels(item.get("labels", []))
        return IssueData(
            number=item["number"],
            title=item.get("title", ""),
            state=item.get("state", "open"),
            labels=labels,
            html_url=item.get("html_url", ""),
            repo_full_name=repo_full_name,
        )

    # --- Raw listing (used by the poller) ---

    async def list_issues_since(self, repo_full_name: str, since: str) -> list[dict[str, Any]]:
        """Issues (open and closed, PRs excluded) updated since an ISO timestamp."""
        items = await self._request_all(
            "/issues",
            repo_full_name=repo_full_name,
            params={
                "state": "all",
                "since": since,
                "sort": "updated",
                "direction": "asc",
                "per_page": 100,
            },
        )
        return [item for item in items if "pull_request" not in item]

    async def list_comments_since(self, repo_full_name: str, since: str) -> list[dict[str, Any]]:
        """Issue comments created/updated since an ISO timestamp, oldest first."""
        return await self._request_all(
            "/issues/comments",
            repo_full_name=repo_full_name,
            params={"since": since, "sort": "updated", "direction": "asc", "per_page": 100},
        )

    async def get_issue_raw(self, issue_number: int, repo_full_name: str) -> dict[str, Any]:
        """Raw issue JSON (webhook-shaped) for building polled events."""
        resp = await self._request("GET", f"/issues/{issue_number}", repo_full_name=repo_full_name)
        return resp.json()

    # --- Issue operations ---

    async def get_sub_issues(
        self, issue_number: int, repo_full_name: str
    ) -> list[IssueData] | None:
        """Fetch sub-issues of a parent; ``None`` when the fetch failed (an
        empty list is a real answer and callers act on it)."""
        url = f"/issues/{issue_number}/sub_issues"
        try:
            items = await self._request_all(
                url, repo_full_name=repo_full_name, params={"per_page": 100}
            )
        except httpx.HTTPStatusError as exc:
            log.warning("Failed to fetch sub-issues for #%d: %s", issue_number, exc)
            return None
        except httpx.TimeoutException:
            log.warning("Timeout fetching sub-issues for #%d", issue_number)
            return None

        return [self._build_issue(item, repo_full_name=repo_full_name) for item in items]

    async def get_issue(self, issue_number: int, repo_full_name: str) -> IssueData:
        """Get a single issue by number."""
        resp = await self._request("GET", f"/issues/{issue_number}", repo_full_name=repo_full_name)
        return self._build_issue(resp.json(), repo_full_name=repo_full_name)

    async def list_issues(
        self,
        state: str = "open",
        labels: list[str] | None = None,
        per_page: int = 50,
        *,
        repo_full_name: str,
    ) -> list[IssueData]:
        """Issues (PRs excluded) filtered by state and labels, in GitHub's order."""
        params: dict[str, str | int] = {
            "state": state,
            "sort": "created",
            "direction": "asc",
            "per_page": per_page,
        }
        if labels:
            params["labels"] = ",".join(labels)

        resp = await self._request("GET", "/issues", repo_full_name=repo_full_name, params=params)
        return [
            self._build_issue(item, repo_full_name=repo_full_name)
            for item in resp.json()
            if "pull_request" not in item
        ]

    async def list_comments(self, issue_number: int, repo_full_name: str) -> list[CommentData]:
        """List comments on an issue."""
        items = await self._request_all(
            f"/issues/{issue_number}/comments",
            repo_full_name=repo_full_name,
            params={"per_page": 100},
        )
        return [
            CommentData(
                id=item.get("id", 0),
                body=item.get("body", ""),
                user_login=item.get("user", {}).get("login", "unknown"),
            )
            for item in items
        ]

    async def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        *,
        repo_full_name: str,
    ) -> IssueData:
        """Create a new issue in ``repo_full_name``."""
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels

        resp = await self._request("POST", "/issues", repo_full_name=repo_full_name, json=payload)
        return self._build_issue(resp.json(), repo_full_name=repo_full_name)

    async def add_comment(self, issue_number: int, body: str, repo_full_name: str) -> CommentData:
        """Add a comment to an issue."""
        resp = await self._request(
            "POST",
            f"/issues/{issue_number}/comments",
            repo_full_name=repo_full_name,
            json={"body": body},
        )
        item = resp.json()
        return CommentData(
            id=item.get("id", 0),
            body=item.get("body", ""),
            user_login=item.get("user", {}).get("login", "unknown"),
        )

    async def update_issue(self, issue_number: int, state: str, repo_full_name: str) -> IssueData:
        """Update an issue's state (open/closed)."""
        resp = await self._request(
            "PATCH",
            f"/issues/{issue_number}",
            repo_full_name=repo_full_name,
            json={"state": state},
        )
        return self._build_issue(resp.json(), repo_full_name=repo_full_name)
