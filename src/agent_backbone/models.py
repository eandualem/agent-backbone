"""Normalised GitHub event models shared by the webhook, the poller and routing."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

_FROM_TAG_PATTERN = re.compile(r"^\[from:([a-z][a-z0-9-]*)\]", re.IGNORECASE)

ISSUE_TYPE_WEIGHTS: dict[str, float] = {
    "spec-gap": 100.0,
    "bug": 90.0,
    "task": 50.0,
    "question": 20.0,
    "optimization": 10.0,
}
"""The issue-type labels the backbone recognises, with their default priority
weight (the ``priority.type_weights`` setting overrides the weights)."""


def parse_from_tag(comment_body: str) -> str | None:
    """Extract entity name from ``[from:X]`` tag at start of comment body.

    Returns the lowercased entity name, or None if no valid tag is found.
    """
    match = _FROM_TAG_PATTERN.match(comment_body.lstrip())
    if match:
        return match.group(1).lower()
    return None


class DeliveryOutcome(StrEnum):
    """What happened to one delivery attempt — the ``outcome`` of every ``deliveries`` row."""

    DELIVERED = "delivered"
    RETRIED = "retried"
    ALREADY_DELIVERED = "already_delivered"
    AWAITING_ACK = "awaiting_ack"
    OFFLINE = "offline"
    WAITING_FOR_HUMAN = "waiting_for_human"
    AGENT_WORKING = "agent_working"
    HUMAN_TYPING = "human_typing"
    SETTLING = "settling"
    DELIVERY_FAILED = "delivery_failed"


SUCCESS_OUTCOMES = frozenset({DeliveryOutcome.DELIVERED, DeliveryOutcome.RETRIED})
"""The message reached the agent."""
BLOCKED_OUTCOMES = frozenset(
    {
        DeliveryOutcome.OFFLINE,
        DeliveryOutcome.WAITING_FOR_HUMAN,
        DeliveryOutcome.AGENT_WORKING,
        DeliveryOutcome.HUMAN_TYPING,
        DeliveryOutcome.SETTLING,
    }
)
"""The agent could not take the message — one per blocking delivery condition."""
RETRYABLE_OUTCOMES = BLOCKED_OUTCOMES | {DeliveryOutcome.DELIVERY_FAILED}
"""Outcomes the retry job re-attempts."""


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
            elif name in ISSUE_TYPE_WEIGHTS:
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
