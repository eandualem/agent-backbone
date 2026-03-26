"""Pydantic models for webhook event validation.

Validates untrusted GitHub webhook payloads into typed structures.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

_FROM_TAG_PATTERN = re.compile(r"^\[from:([a-z][a-z0-9-]*)\]", re.IGNORECASE)


def parse_from_tag(comment_body: str) -> str | None:
    """Extract entity name from ``[from:X]`` tag at start of comment body.

    Returns the lowercased entity name, or None if no valid tag is found.
    """
    match = _FROM_TAG_PATTERN.match(comment_body.lstrip())
    if match:
        return match.group(1).lower()
    return None


_GOVERNANCE_TAG_PATTERN = re.compile(r"\[governance:([a-zA-Z0-9_.]+)\]")


def parse_governance_tag(comment_body: str) -> str | None:
    """Extract event type from ``[governance:X]`` tag in comment body.

    Unlike parse_from_tag, this scans the full body (not anchored to start).
    Returns the event type string, or None if no valid tag is found.
    """
    match = _GOVERNANCE_TAG_PATTERN.search(comment_body)
    if match:
        return match.group(1)
    return None


def normalize_repo_full_name(repo_full_name: str | None) -> str:
    """Normalize owner/repo strings for persistence and dedup keys."""
    return (repo_full_name or "").strip()


def repo_session_name(repo_full_name: str | None) -> str:
    """Return the repo-session name implied by an owner/repo identity."""
    normalized = normalize_repo_full_name(repo_full_name)
    if "/" in normalized:
        return normalized.split("/", 1)[1].strip()
    return normalized


def acknowledgment_entities(
    target_entity: str,
    repo_full_name: str | None = None,
    *,
    session_name: str | None = None,
) -> tuple[str, ...]:
    """Canonical acknowledgment identities for a delivery target.

    Repo-local issue delivery targets are stored as the concrete repo session
    name (for example ``agent-orchestration-dashboard``), while the coding
    agent comments with ``[from:coding-agent]``. Treat both as the same ack
    identity for repo-local queues.
    """
    aliases: list[str] = [target_entity]
    if session_name and session_name not in aliases:
        aliases.append(session_name)

    repo_session = repo_session_name(repo_full_name)
    if repo_session and (target_entity == repo_session or session_name == repo_session):
        aliases.append("coding-agent")

    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def issue_identity_key(issue_number: int, repo_full_name: str | None = None) -> tuple[str, int]:
    """Build the canonical repo-qualified issue identity."""
    return (normalize_repo_full_name(repo_full_name), issue_number)


class EventType(StrEnum):
    """Normalized event types from GitHub webhooks."""

    ISSUE_OPENED = "issue_opened"
    ISSUE_LABELED = "issue_labeled"
    ISSUE_CLOSED = "issue_closed"
    COMMENT_CREATED = "comment_created"
    PULL_REQUEST_OPENED = "pull_request_opened"
    UNKNOWN = "unknown"

    @classmethod
    def from_github(cls, event_type: str, action: str) -> EventType:
        """Map GitHub event_type + action to our enum."""
        if event_type == "issues":
            mapping = {
                "opened": cls.ISSUE_OPENED,
                "labeled": cls.ISSUE_LABELED,
                "closed": cls.ISSUE_CLOSED,
            }
            return mapping.get(action, cls.UNKNOWN)
        if event_type == "issue_comment" and action == "created":
            return cls.COMMENT_CREATED
        if event_type == "pull_request":
            mapping = {
                "opened": cls.PULL_REQUEST_OPENED,
                "ready_for_review": cls.PULL_REQUEST_OPENED,
                "reopened": cls.PULL_REQUEST_OPENED,
            }
            return mapping.get(action, cls.UNKNOWN)
        return cls.UNKNOWN


class ParsedLabels(BaseModel):
    """Extracted label information from an issue."""

    sender: str = "unknown"
    targets: list[str] = Field(default_factory=list)
    issue_type: str = ""
    priority: str = ""

    @classmethod
    def from_github_labels(cls, labels: list[dict]) -> ParsedLabels:
        """Parse GitHub label objects into structured data."""
        sender = "unknown"
        targets: list[str] = []
        issue_type = ""
        priority = ""

        for label in labels:
            name = label.get("name", "")
            if name.startswith("from:"):
                sender = name[5:]
            elif name.startswith("for:"):
                targets.append(name[4:])
            elif name in ("spec-gap", "task", "question", "bug", "optimization"):
                issue_type = name
            elif name in ("blocking", "non-blocking"):
                priority = name

        return cls(sender=sender, targets=targets, issue_type=issue_type, priority=priority)


class IssueData(BaseModel):
    """Minimal issue data extracted from webhook payload."""

    number: int
    title: str = ""
    state: str = "open"
    labels: ParsedLabels = Field(default_factory=ParsedLabels)
    html_url: str = ""
    repo_full_name: str = ""
    is_pull_request: bool = False

    @property
    def identity_key(self) -> tuple[str, int]:
        """Canonical repo-qualified identity for this issue."""
        return issue_identity_key(self.number, self.repo_full_name)


class CommentData(BaseModel):
    """Comment data from issue_comment webhook events."""

    id: int = 0
    body: str = ""
    user_login: str = "unknown"


class IssueEvent(BaseModel):
    """Normalized webhook event ready for flow processing."""

    event_type: EventType
    issue: IssueData
    comment: CommentData | None = None
    delivery_id: str = ""

    @classmethod
    def from_webhook(
        cls,
        event_type_str: str,
        action: str,
        payload: dict,
        delivery_id: str = "",
    ) -> IssueEvent:
        """Construct from raw GitHub webhook data."""
        event_type = EventType.from_github(event_type_str, action)
        issue_key = "pull_request" if event_type_str == "pull_request" else "issue"
        issue_data = payload.get(issue_key, {})
        repository = payload.get("repository", {})
        labels = ParsedLabels.from_github_labels(issue_data.get("labels", []))

        issue = IssueData(
            number=issue_data.get("number", 0),
            title=issue_data.get("title", ""),
            state=issue_data.get("state", "open"),
            labels=labels,
            html_url=issue_data.get("html_url", ""),
            repo_full_name=repository.get("full_name", ""),
            is_pull_request=issue_key == "pull_request",
        )

        comment = None
        comment_data = payload.get("comment")
        if comment_data:
            comment = CommentData(
                id=comment_data.get("id", 0),
                body=comment_data.get("body", ""),
                user_login=comment_data.get("user", {}).get("login", "unknown"),
            )

        return cls(
            event_type=event_type,
            issue=issue,
            comment=comment,
            delivery_id=delivery_id,
        )
