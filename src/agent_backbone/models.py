"""Normalised GitHub event models shared by the webhook, the poller and routing."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

# The same vocabulary as ``sanitize_name``: an agent called app_test or 1st-desk
# must be able to acknowledge.
_FROM_TAG_PATTERN = re.compile(r"^\[from:([A-Za-z0-9][A-Za-z0-9_.-]*)\]")

# Reporting is a bounded authoring tool. These limits apply at every entry point,
# not just to how much a UI happens to show.
REPORT_BODY_BYTES = 16_384
REPORT_TEXT_CHARACTERS = 1_500
REPORT_LINKS = 6
REPORTS_PER_HOUR = 30


def report_error_details(errors: list[dict]) -> list[dict]:
    """Bounded errors without rejected input, even for malicious extra field names."""
    return [
        {
            "field": ".".join(map(str, error["loc"]))
            .encode("unicode_escape")
            .decode("ascii")[:160],
            "message": error["msg"][:240],
        }
        for error in errors[:10]
    ]


def _report_text(value: str) -> str:
    if not value.strip():
        raise ValueError("write a short sentence; this field cannot be blank")
    if any(
        unicodedata.category(c) in {"Cc", "Cs"}
        or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for c in value
    ):
        raise ValueError("use one plain-text paragraph without control characters")
    return value.strip()


ReportText = Annotated[str, AfterValidator(_report_text)]
ReportAgentName = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
]


class ReportLink(BaseModel):
    """A short title and a real destination, attached to the section it explains."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: ReportText = Field(min_length=1, max_length=60)
    url: str = Field(min_length=1, max_length=400)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        # Links are displayed, never fetched. Credentials and control characters
        # have no place in a team report, even in an otherwise valid HTTPS URL.
        if any(c.isspace() or unicodedata.category(c) in {"Cc", "Cs"} for c in value):
            raise ValueError("use an absolute HTTP(S) link without whitespace")
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("use an absolute HTTP(S) link without credentials") from exc
        return value


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: ReportText = Field(min_length=1, max_length=240)
    links: list[ReportLink] = Field(
        max_length=2, description="Up to two titled links; use [] when none is relevant."
    )


class ReportProgress(ReportSection):
    text: ReportText = Field(min_length=1, max_length=480)


class ReportNext(ReportSection):
    text: ReportText = Field(min_length=1, max_length=320)


class ReportBlockers(ReportNext):
    kind: Literal["none", "owner", "dependency", "other"] = Field(
        description="None, an owner's decision/action, a review/dependency, or another obstacle."
    )


class ProgressReport(BaseModel):
    """A conversational account for a teammate unfamiliar with the implementation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["active", "blocked", "complete", "inactive"] = Field(
        description="Authored work status, separate from the measured runtime state."
    )
    goal: ReportSection = Field(description="What are we trying to make better, and for whom?")
    progress: ReportProgress = Field(
        description="What useful result changed since the last report?"
    )
    blockers: ReportBlockers = Field(
        description="What is needed to continue? Say 'None.' if clear."
    )
    next: ReportNext = Field(description="What happens next? Say explicitly if nothing is active.")
    note: ReportSection | None = Field(
        default=None, description="Optional useful learning, suggestion, or observation."
    )

    def sections(self) -> list[tuple[str, ReportSection]]:
        return [
            (key, section)
            for key in ("goal", "progress", "blockers", "next", "note")
            if (section := getattr(self, key)) is not None
        ]

    @model_validator(mode="after")
    def bounded_report(self) -> ProgressReport:
        sections = [section for _, section in self.sections()]
        links = [link for section in sections for link in section.links]
        characters = sum(len(s.text) for s in sections) + sum(len(link.title) for link in links)
        if characters > REPORT_TEXT_CHARACTERS:
            raise ValueError(
                f"report text and link titles total {characters} characters; "
                f"maximum {REPORT_TEXT_CHARACTERS}. Shorten the report."
            )
        if len(links) > REPORT_LINKS:
            raise ValueError(f"report has {len(links)} links; maximum {REPORT_LINKS}")
        if self.status == "blocked" and self.blockers.kind == "none":
            raise ValueError("a blocked report must explain a blocker and its kind")
        if self.status in {"complete", "inactive"} and self.blockers.kind != "none":
            raise ValueError("complete/inactive reports cannot have an unresolved blocker")
        return self


class PublishReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent: ReportAgentName
    request_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description="Reuse this key and the same report when retrying a publication.",
    )
    report: ProgressReport


class ReportQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agents: list[ReportAgentName] = Field(default_factory=list, max_length=20)
    author_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    history: bool = False
    members: bool = False
    limit: int = Field(default=5, ge=1, le=20)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def author_history(self) -> ReportQuery:
        if self.author_id and (not self.history or self.agents):
            raise ValueError("author_id requires history and cannot be combined with agents")
        return self


def report_example() -> dict:
    """Synthetic authoring example, shared by the CLI and API schema tool."""
    return {
        "status": "active",
        "goal": {
            "text": "Make checkout easier to use so customers can finish their orders.",
            "links": [
                {"title": "Simpler checkout", "url": "https://github.com/example/shop/issues/42"}
            ],
        },
        "progress": {
            "text": "Customers now understand why a payment failed. The change passes our checks.",
            "links": [
                {"title": "Payment feedback", "url": "https://github.com/example/shop/pull/43"}
            ],
        },
        "blockers": {"kind": "none", "text": "None.", "links": []},
        "next": {
            "text": "I'll check the mobile experience and prepare the change for review.",
            "links": [],
        },
    }


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
    NOT_WAITING = "not_waiting"
    """A plan response with no plan on screen to answer — refused, never queued."""
    EXPIRED = "expired"
    """A queued message dropped after ``timing.queue_expiry_minutes`` — terminal, never retried."""


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
    REVIEW_SUBMITTED = "review_submitted"
    REVIEW_STARTED = "review_started"
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
        if event_type == "pull_request_review" and action == "submitted":
            # One event per review. Its inline comments arrive separately as
            # ``pull_request_review_comment`` and are deliberately not routed:
            # every inline comment belongs to a review, so the review is the
            # signal and the agent reads the details on GitHub.
            return cls.REVIEW_SUBMITTED
        return cls.UNKNOWN


class ParsedLabels(BaseModel):
    """Extracted label information from an issue."""

    sender: str = "unknown"
    targets: list[str] = Field(default_factory=list)
    issue_type: str = ""
    priority: str = ""
    """``blocking``, ``non-blocking`` or empty."""

    @property
    def blocking(self) -> bool:
        return self.priority == "blocking"

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
    """An issue (or pull request) as routing sees it.

    ``repo_full_name`` is always set: issue-scoped data is keyed by
    ``(repo, number)`` everywhere, never by the number alone.
    """

    number: int
    repo_full_name: str
    title: str = ""
    state: str = "open"
    labels: ParsedLabels = Field(default_factory=ParsedLabels)
    html_url: str = ""
    created_at: str = ""
    closed_at: str = ""
    is_pull_request: bool = False
    body: str = ""
    """The issue or pull request body (a pull request's ``Closes #N`` lives here)."""
    head_ref: str = ""
    """A pull request's head branch — how the backbone recognises one an agent opened."""
    head_repo: str = ""
    """The repository the head branch lives in (a fork, or the base repository)."""

    def linked_issues(self) -> list[int]:
        """Issues this body closes (``Closes #N``, ``Fixes #N``, ``Resolves #N``)."""
        seen: list[int] = []
        for match in _CLOSES_RE.finditer(self.body or ""):
            number = int(match.group(1))
            if number not in seen:
                seen.append(number)
        return seen


_CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d{1,7})\b", re.I)


class CommentData(BaseModel):
    """Comment data from issue_comment webhook events."""

    id: int = 0
    body: str = ""
    user_login: str = "unknown"


class ReviewData(BaseModel):
    """A pull request review from ``pull_request_review`` webhook events."""

    id: int = 0
    body: str = ""
    user_login: str = "unknown"
    state: str = ""
    """``approved``, ``changes_requested`` or ``commented`` (GitHub's spelling)."""
    html_url: str = ""
    commit_id: str = ""
    """The commit the review looked at — what makes a late or replayed review
    recognisable as one of an earlier push."""
    submitted_at: str = ""
    head_sha: str = ""


def review_source_key(issue: IssueData, review: ReviewData) -> str:
    reviewer = review.user_login.casefold().removesuffix("[bot]")
    prefix = f"review-start:{issue.repo_full_name.casefold()}#{issue.number}"
    return f"{prefix}:{reviewer}:{review.commit_id}"


class IssueEvent(BaseModel):
    """Normalized webhook event ready for flow processing."""

    event_type: EventType
    issue: IssueData
    comment: CommentData | None = None
    review: ReviewData | None = None
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
        issue_key = "pull_request" if event_type_str.startswith("pull_request") else "issue"
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
            body=issue_data.get("body") or "",
            created_at=issue_data.get("created_at") or "",
            closed_at=issue_data.get("closed_at") or "",
            is_pull_request=issue_key == "pull_request" or "pull_request" in issue_data,
            head_ref=((issue_data.get("head") or {}).get("ref") or ""),
            head_repo=(((issue_data.get("head") or {}).get("repo") or {}).get("full_name") or ""),
        )

        comment = None
        comment_data = payload.get("comment")
        if comment_data:
            comment = CommentData(
                id=comment_data.get("id", 0),
                body=comment_data.get("body", ""),
                user_login=comment_data.get("user", {}).get("login", "unknown"),
            )

        review = None
        review_data = payload.get("review")
        if review_data:
            review = ReviewData(
                id=review_data.get("id", 0),
                body=review_data.get("body") or "",
                user_login=(review_data.get("user") or {}).get("login", "unknown"),
                state=(review_data.get("state") or "").lower(),
                html_url=review_data.get("html_url", ""),
                commit_id=review_data.get("commit_id") or "",
                submitted_at=review_data.get("submitted_at") or "",
                head_sha=(issue_data.get("head") or {}).get("sha") or "",
            )

        return cls(
            event_type=event_type,
            issue=issue,
            comment=comment,
            review=review,
            delivery_id=delivery_id,
        )
