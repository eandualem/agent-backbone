"""GitHub REST API client for querying issues.

Uses httpx for async HTTP and authenticates every repository request through
GitHub App installation tokens derived from the configured App credentials.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from agent_backbone.models import CommentData, IssueData, ParsedLabels
from agent_backbone.services.github.exceptions import GitHubServiceError

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
_TOKEN_REFRESH_SKEW = timedelta(minutes=1)
_MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
_SAFE_RETRY_METHODS = {"GET"}
_TRANSIENT_SERVER_ERRORS = {500, 502, 503, 504}
_MAX_AUTH_RETRIES = 1
_MAX_RATE_LIMIT_RETRIES = 1
_MAX_SAFE_REQUEST_RETRIES = 2
_MUTATION_MIN_INTERVAL_SECONDS = 1.0
_MAX_AUTOMATIC_RATE_LIMIT_WAIT_SECONDS = 5.0
_SAFE_RETRY_BACKOFF_SECONDS = (0.5, 1.0)


@dataclass(slots=True)
class _CachedInstallationToken:
    token: str
    expires_at: datetime


def _issue_sort_key(issue: IssueData) -> tuple[int, int]:
    """Sort key for issue delivery ordering: lower number = older = higher priority."""
    return (issue.number, 0)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_github_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(UTC)


def _next_link(resp: httpx.Response) -> str | None:
    """Return the next-page URL from a paginated GitHub response, if present."""
    next_link = resp.links.get("next")
    if not isinstance(next_link, dict):
        return None
    href = next_link.get("url")
    return href if isinstance(href, str) and href else None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GitHubClient:
    """Async GitHub REST API client.

    Usage:
        async with GitHubClient(config) as gh:
            issues = await gh.list_open_issues("for:ike")
    """

    def __init__(self, config: BackboneConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._private_key: Any | None = None
        self._installation_ids: dict[str, int] = {}
        self._installation_tokens: dict[str, _CachedInstallationToken] = {}
        self._mutation_lock = asyncio.Lock()
        self._last_mutation_at = 0.0

    async def __aenter__(self) -> GitHubClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # --- LifecycleAware methods ---

    async def start(self) -> None:
        """Initialize the HTTP client (LifecycleAware)."""
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
        self._last_mutation_at = 0.0

    async def health_check(self) -> dict:
        """Check GitHub API connectivity."""
        return {
            "healthy": self._client is not None,
            "service": "github",
            "client_active": self._client is not None,
            "auth_mode": "github_app",
        }

    def _default_repo_full_name(self) -> str:
        return f"{self._config.github.owner}/{self._config.github.repo}"

    def _resolve_repo(self, repo_full_name: str | None = None) -> tuple[str, str]:
        if repo_full_name and "/" in repo_full_name:
            owner, repo = repo_full_name.split("/", 1)
            if owner and repo:
                return owner, repo
        return (self._config.github.owner, self._config.github.repo)

    def _repo_key(self, repo_full_name: str | None = None) -> str:
        owner, repo = self._resolve_repo(repo_full_name)
        return f"{owner}/{repo}"

    def _invalidate_installation_token(self, repo_full_name: str | None = None) -> None:
        self._installation_tokens.pop(self._repo_key(repo_full_name), None)

    def _validate_app_config(self) -> None:
        missing: list[str] = []
        if not self._config.github_app_id:
            missing.append("GITHUB_APP_ID")
        if not self._config.github_app_private_key_path:
            missing.append("GITHUB_APP_PRIVATE_KEY_PATH")
        if missing:
            missing_str = ", ".join(missing)
            raise RuntimeError(
                f"GitHub App auth requires {missing_str}; PAT auth is no longer supported"
            )

        key_path = Path(self._config.github_app_private_key_path).expanduser()
        if not key_path.is_file():
            raise RuntimeError(f"GitHub App private key not found: {key_path}")

    def _load_private_key(self) -> Any:
        if self._private_key is not None:
            return self._private_key

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

    async def _get_installation_id(self, repo_full_name: str | None = None) -> int:
        repo_key = self._repo_key(repo_full_name)
        cached = self._installation_ids.get(repo_key)
        if cached is not None:
            return cached

        owner, repo = self._resolve_repo(repo_full_name)
        resp = await self._app_request("GET", f"/repos/{owner}/{repo}/installation")
        resp.raise_for_status()
        installation_id = int(resp.json()["id"])
        self._installation_ids[repo_key] = installation_id
        return installation_id

    async def _get_installation_token(self, repo_full_name: str | None = None) -> str:
        repo_key = self._repo_key(repo_full_name)
        cached = self._installation_tokens.get(repo_key)
        if cached and cached.expires_at > (_utcnow() + _TOKEN_REFRESH_SKEW):
            return cached.token

        installation_id = await self._get_installation_id(repo_full_name)
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

        expires_at = _parse_github_timestamp(expires_at_raw)
        self._installation_tokens[repo_key] = _CachedInstallationToken(
            token=token,
            expires_at=expires_at,
        )
        return token

    async def _request(
        self,
        method: str,
        url: str,
        *,
        repo_full_name: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        assert self._client is not None
        method = method.upper()
        base_headers = dict(kwargs.pop("headers", {}))
        safe_retry_count = 0
        auth_retry_count = 0
        rate_limit_retry_count = 0

        while True:
            headers = dict(base_headers)
            token = await self._get_installation_token(repo_full_name)
            headers["Authorization"] = f"Bearer {token}"

            try:
                resp = await self._send_request(method, url, headers=headers, **kwargs)
            except httpx.TimeoutException as exc:
                if method in _SAFE_RETRY_METHODS and safe_retry_count < _MAX_SAFE_REQUEST_RETRIES:
                    delay = _SAFE_RETRY_BACKOFF_SECONDS[safe_retry_count]
                    safe_retry_count += 1
                    log.warning(
                        "GitHub %s %s timed out; retrying in %.1fs (%d/%d)",
                        method,
                        url,
                        delay,
                        safe_retry_count,
                        _MAX_SAFE_REQUEST_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise GitHubServiceError(
                    f"GitHub request timed out for {method} {url}",
                    retry_allowed=True,
                ) from exc
            except httpx.RequestError as exc:
                if method in _SAFE_RETRY_METHODS and safe_retry_count < _MAX_SAFE_REQUEST_RETRIES:
                    delay = _SAFE_RETRY_BACKOFF_SECONDS[safe_retry_count]
                    safe_retry_count += 1
                    log.warning(
                        "GitHub %s %s failed (%s); retrying in %.1fs (%d/%d)",
                        method,
                        url,
                        exc.__class__.__name__,
                        delay,
                        safe_retry_count,
                        _MAX_SAFE_REQUEST_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise GitHubServiceError(
                    f"GitHub request failed for {method} {url}: {exc}",
                    retry_allowed=True,
                ) from exc

            if resp.status_code == 401 and auth_retry_count < _MAX_AUTH_RETRIES:
                auth_retry_count += 1
                self._invalidate_installation_token(repo_full_name)
                log.warning(
                    "GitHub rejected installation token for %s; "
                    "refreshing token and retrying %s %s",
                    self._repo_key(repo_full_name),
                    method,
                    url,
                )
                continue

            retry_after = self._rate_limit_retry_after(resp)
            if retry_after is not None and rate_limit_retry_count < _MAX_RATE_LIMIT_RETRIES:
                if retry_after <= _MAX_AUTOMATIC_RATE_LIMIT_WAIT_SECONDS:
                    rate_limit_retry_count += 1
                    log.warning(
                        "GitHub rate-limited %s %s; retrying in %.1fs (%d/%d)",
                        method,
                        url,
                        retry_after,
                        rate_limit_retry_count,
                        _MAX_RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(retry_after)
                    continue

            if (
                method in _SAFE_RETRY_METHODS
                and resp.status_code in _TRANSIENT_SERVER_ERRORS
                and safe_retry_count < _MAX_SAFE_REQUEST_RETRIES
            ):
                delay = _SAFE_RETRY_BACKOFF_SECONDS[safe_retry_count]
                safe_retry_count += 1
                log.warning(
                    "GitHub %s %s returned %d; retrying in %.1fs (%d/%d)",
                    method,
                    url,
                    resp.status_code,
                    delay,
                    safe_retry_count,
                    _MAX_SAFE_REQUEST_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            if resp.is_error:
                raise self._build_service_error(resp, method, url, retry_after=retry_after)

            return resp

    async def _send_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None
        if method not in _MUTATING_METHODS:
            return await self._client.request(method, url, **kwargs)

        async with self._mutation_lock:
            delay = _MUTATION_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_mutation_at)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await self._client.request(method, url, **kwargs)
            finally:
                self._last_mutation_at = time.monotonic()

    def _rate_limit_retry_after(self, resp: httpx.Response) -> float | None:
        if resp.status_code not in {403, 429}:
            return None

        retry_after_header = resp.headers.get("Retry-After")
        if retry_after_header:
            try:
                return max(float(retry_after_header), 0.0)
            except ValueError:
                pass

        if resp.headers.get("X-RateLimit-Remaining") == "0":
            reset_header = resp.headers.get("X-RateLimit-Reset")
            if reset_header:
                try:
                    return max(float(reset_header) - time.time(), 0.0)
                except ValueError:
                    pass

        if self._is_secondary_rate_limit(resp):
            return 60.0

        return None

    def _is_secondary_rate_limit(self, resp: httpx.Response) -> bool:
        try:
            payload = resp.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str) and "secondary rate limit" in message.lower():
                return True
        return "secondary rate limit" in resp.text.lower()

    def _build_service_error(
        self,
        resp: httpx.Response,
        method: str,
        url: str,
        *,
        retry_after: float | None = None,
    ) -> GitHubServiceError:
        message = f"GitHub API {resp.status_code} for {method} {url}"
        try:
            payload = resp.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            upstream_message = payload.get("message")
            if isinstance(upstream_message, str) and upstream_message:
                message = f"{message}: {upstream_message}"

        retry_allowed = (
            resp.status_code >= 500 or retry_after is not None or resp.status_code == 401
        )
        return GitHubServiceError(
            message,
            status_code=resp.status_code,
            retry_after_seconds=retry_after,
            response_text=resp.text[:500],
            retry_allowed=retry_allowed,
        )

    def _build_issue(self, item: dict[str, Any], repo_full_name: str | None = None) -> IssueData:
        labels = ParsedLabels.from_github_labels(item.get("labels", []))
        return IssueData(
            number=item["number"],
            title=item.get("title", ""),
            body=item.get("body", "") or "",
            state=item.get("state", "open"),
            labels=labels,
            html_url=item.get("html_url", ""),
            repo_full_name=repo_full_name or self._default_repo_full_name(),
            user_login=item.get("user", {}).get("login", "unknown"),
            created_at=item.get("created_at", "") or "",
            updated_at=item.get("updated_at", "") or "",
            comment_count=item.get("comments", 0) or 0,
            is_pull_request="pull_request" in item,
        )

    def _build_comment(
        self,
        item: dict[str, Any],
        *,
        repo_full_name: str,
        issue_number: int,
    ) -> CommentData:
        return CommentData(
            id=item.get("id", 0),
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            body=item.get("body", ""),
            user_login=item.get("user", {}).get("login", "unknown"),
            created_at=item.get("created_at", "") or "",
            updated_at=item.get("updated_at", "") or "",
        )

    async def _list_paginated_items(
        self,
        url: str,
        *,
        repo_full_name: str | None = None,
        params: dict[str, str | int] | None = None,
        max_pages: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch and combine all pages from a GitHub list endpoint.

        Args:
            max_pages: Stop after this many pages. 0 means fetch all pages.
        """
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params = dict(params or {})
        page_count = 0

        while next_url:
            resp = await self._request(
                "GET",
                next_url,
                repo_full_name=repo_full_name,
                params=next_params or None,
            )
            payload = resp.json()
            if isinstance(payload, list):
                items.extend(item for item in payload if isinstance(item, dict))
            page_count += 1
            if max_pages and page_count >= max_pages:
                break
            next_url = _next_link(resp)
            next_params = {}

        return items

    # --- Repository operations ---

    async def list_owner_repos(self) -> list[str]:
        """List all repo full names accessible to the GitHub App installation."""
        items: list[dict[str, Any]] = []
        next_url: str | None = "/installation/repositories"
        next_params: dict[str, str | int] = {"per_page": 100}

        while next_url:
            resp = await self._request(
                "GET", next_url, params=next_params or None,
            )
            payload = resp.json()
            for repo in payload.get("repositories", []):
                if isinstance(repo, dict):
                    items.append(repo)
            next_url = _next_link(resp)
            next_params = {}

        return sorted(
            item["full_name"]
            for item in items
            if isinstance(item.get("full_name"), str)
            and not item.get("archived")
        )

    # --- Issue operations ---

    async def list_open_issues(
        self, label: str, repo_full_name: str | None = None
    ) -> list[IssueData]:
        """List open issues with a specific label, sorted for delivery."""
        issues = await self.list_issues(
            state="open",
            labels=[label],
            per_page=100,
            repo_full_name=repo_full_name,
        )
        issues.sort(key=lambda i: _issue_sort_key(i))
        return issues

    async def get_sub_issues(
        self, issue_number: int, repo_full_name: str | None = None
    ) -> list[IssueData]:
        """Fetch sub-issues of a parent.

        Returns empty list on error (404, timeout, rate limit).
        """
        owner, repo = self._resolve_repo(repo_full_name)
        url = f"/repos/{owner}/{repo}/issues/{issue_number}/sub_issues"
        try:
            resp = await self._request("GET", url, repo_full_name=repo_full_name)
        except GitHubServiceError as exc:
            log.warning("Failed to fetch sub-issues for #%d: %s", issue_number, exc)
            return []

        return [self._build_issue(item, repo_full_name=repo_full_name) for item in resp.json()]

    async def count_open_sub_issues(
        self, issue_number: int, repo_full_name: str | None = None
    ) -> int:
        """Count open sub-issues of a parent. Returns 0 on error."""
        subs = await self.get_sub_issues(issue_number, repo_full_name=repo_full_name)
        return sum(1 for sub in subs if sub.state == "open")

    async def get_issue(self, issue_number: int, repo_full_name: str | None = None) -> IssueData:
        """Get a single issue by number."""
        owner, repo = self._resolve_repo(repo_full_name)
        url = f"/repos/{owner}/{repo}/issues/{issue_number}"
        resp = await self._request("GET", url, repo_full_name=repo_full_name)
        return self._build_issue(resp.json(), repo_full_name=repo_full_name)

    async def list_issues(
        self,
        state: str = "open",
        labels: list[str] | None = None,
        per_page: int = 50,
        repo_full_name: str | None = None,
        sort: str = "created",
        direction: str = "asc",
        max_pages: int = 0,
    ) -> list[IssueData]:
        """List issues with flexible filtering by state and labels."""
        owner, repo = self._resolve_repo(repo_full_name)
        url = f"/repos/{owner}/{repo}/issues"
        params: dict[str, str | int] = {
            "state": state,
            "sort": sort,
            "direction": direction,
            "per_page": per_page,
        }
        if labels:
            params["labels"] = ",".join(labels)

        issues: list[IssueData] = []
        for item in await self._list_paginated_items(
            url,
            repo_full_name=repo_full_name,
            params=params,
            max_pages=max_pages,
        ):
            if "pull_request" in item:
                continue
            issues.append(self._build_issue(item, repo_full_name=repo_full_name))

        issues.sort(key=lambda issue: _issue_sort_key(issue))
        return issues

    async def list_comments(
        self, issue_number: int, repo_full_name: str | None = None
    ) -> list[CommentData]:
        """List comments on an issue."""
        repo_key = self._repo_key(repo_full_name)
        owner, repo = self._resolve_repo(repo_key)
        url = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        return [
            self._build_comment(
                item,
                repo_full_name=repo_key,
                issue_number=issue_number,
            )
            for item in await self._list_paginated_items(
                url,
                repo_full_name=repo_key,
                params={"per_page": 100},
            )
        ]

    async def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        repo_full_name: str | None = None,
    ) -> IssueData:
        """Create a new issue in the requested repo or the default coordination repo."""
        repo_full_name = self._repo_key(repo_full_name)
        owner, repo = self._resolve_repo(repo_full_name)
        url = f"/repos/{owner}/{repo}/issues"
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels

        resp = await self._request("POST", url, repo_full_name=repo_full_name, json=payload)
        item = resp.json()
        return self._build_issue(item, repo_full_name=repo_full_name)

    async def add_comment(
        self,
        issue_number: int,
        body: str,
        repo_full_name: str | None = None,
    ) -> CommentData:
        """Add a comment to an issue in the requested repo."""
        repo_full_name = self._repo_key(repo_full_name)
        owner, repo = self._resolve_repo(repo_full_name)
        url = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        resp = await self._request(
            "POST",
            url,
            repo_full_name=repo_full_name,
            json={"body": body},
        )
        item = resp.json()
        return self._build_comment(
            item,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
        )

    async def update_issue(
        self,
        issue_number: int,
        state: str,
        repo_full_name: str | None = None,
    ) -> IssueData:
        """Update an issue's state (open/closed) in the requested repo."""
        repo_full_name = self._repo_key(repo_full_name)
        owner, repo = self._resolve_repo(repo_full_name)
        url = f"/repos/{owner}/{repo}/issues/{issue_number}"
        resp = await self._request(
            "PATCH",
            url,
            repo_full_name=repo_full_name,
            json={"state": state},
        )
        item = resp.json()
        return self._build_issue(item, repo_full_name=repo_full_name)
