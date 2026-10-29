"""Shared models for GitHub events, deliveries and authored progress reports."""

from __future__ import annotations

import json
import re
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

# The same vocabulary as ``sanitize_name``: an agent called app_test or 1st-desk
# must be able to acknowledge.
_FROM_TAG_PATTERN = re.compile(r"^\[from:([A-Za-z0-9][A-Za-z0-9_.-]*)\]")

# Reporting is a bounded authoring tool. These limits apply at every entry point,
# not just to how much a UI happens to show.
REPORT_BODY_BYTES = 16_384
RESPONSE_BODY_BYTES = 65_536
SETUP_BODY_BYTES = 32_768
SETUP_CANCEL_BODY_BYTES = SETUP_BODY_BYTES + len("cancel_")
REPORT_TEXT_CHARACTERS = 1_500
REPORT_LINKS = 6
REPORTS_PER_HOUR = 30


def _setup_path(value: str) -> str:
    if not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("use an absolute path without parent traversal")
    if any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} for c in value):
        raise ValueError("path must not contain control characters")
    return value


SetupPath = Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(_setup_path)]
SetupRuntime = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9-]*$")]


class SetupBase(BaseModel):
    """Transient operator input, never a required file in an agent's repository."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    data_dir: SetupPath

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class SetupInitialize(SetupBase):
    action: Literal["initialize"]
    service: Literal["user", "none"]
    starter_profile: Literal["core-v1"]


class SetupTemplateBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    template: Literal["orchestrator-v1"]


class SetupCustomBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=16_000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("brief must contain text")
        return value


class SetupAgent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    directory: SetupPath
    create_directory: bool
    runtime: SetupRuntime
    model: str | None = Field(max_length=500)
    brief: SetupTemplateBrief | SetupCustomBrief


class SetupFirstAgent(SetupBase):
    action: Literal["first_agent"]
    hooks: list[SetupRuntime] = Field(min_length=1, max_length=7)
    agent: SetupAgent
    start_greeting: bool

    @model_validator(mode="after")
    def selected_hooks(self) -> SetupFirstAgent:
        if len(set(self.hooks)) != len(self.hooks):
            raise ValueError("hooks must contain unique runtime IDs")
        if self.agent.runtime not in self.hooks:
            raise ValueError("hooks must include the selected agent runtime")
        return self


class SetupCancelFirstAgent(SetupFirstAgent):
    """Cancel the exact saved first-agent input, never an agent or its terminal."""

    action: Literal["cancel_first_agent"]

    def original(self) -> SetupFirstAgent:
        return SetupFirstAgent.model_validate({**self.model_dump(), "action": "first_agent"})


SetupRequest = Annotated[
    SetupInitialize | SetupFirstAgent | SetupCancelFirstAgent, Field(discriminator="action")
]


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
        unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"}
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


class AgentResponse(BaseModel):
    """An explicitly associated answer, independent of progress reporting."""

    model_config = ConfigDict(extra="forbid", strict=True)

    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=6000)
    action: str = Field(min_length=1, max_length=1000)

    @field_validator("question", "answer", "action")
    @classmethod
    def plain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("this field cannot be blank")
        if any(
            (unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} and c not in "\n\t")
            or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
            for c in value
        ):
            raise ValueError("use plain text without control characters")
        return value.strip()


class PublishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent: ReportAgentName
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    response: AgentResponse
    supersedes_id: int | None = Field(default=None, ge=1, le=2**63 - 1)


DOCUMENT_HTML_BYTES = 4_194_304
DOCUMENT_METADATA_BYTES = 8_192
DOCUMENT_WIRE_BYTES = 6 * DOCUMENT_HTML_BYTES + DOCUMENT_METADATA_BYTES
DOCUMENT_STORAGE_BYTES = 1_073_741_824
DOCUMENTS_PER_AUTHOR_HOUR = 30
DOCUMENTS_PER_HOUR = 120


class PublishDocument(BaseModel):
    """Exact HTML with a bounded publication envelope; no HTML normalization."""

    model_config = ConfigDict(extra="forbid", strict=True)

    author: ReportAgentName
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    title: str = Field(min_length=1, max_length=240)
    html: str = Field(min_length=1)
    supersedes_id: int | None = Field(default=None, ge=1, le=2**63 - 1)

    @field_validator("title")
    @classmethod
    def title_text(cls, value: str) -> str:
        _report_text(value)
        return value

    @model_validator(mode="after")
    def byte_limits(self) -> PublishDocument:
        try:
            size = len(self.html.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("HTML must contain valid UTF-8 text") from exc
        if size > DOCUMENT_HTML_BYTES:
            raise ValueError(f"HTML exceeds {DOCUMENT_HTML_BYTES} UTF-8 bytes")
        if self.metadata_bytes() > DOCUMENT_METADATA_BYTES:
            raise ValueError(f"metadata exceeds {DOCUMENT_METADATA_BYTES} JSON bytes")
        return self

    def metadata_bytes(self) -> int:
        return len(json.dumps(self.model_dump() | {"html": ""}, ensure_ascii=True).encode())


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


RETIREMENT_REASONS = frozenset(
    {
        "registration_changed",
        "acknowledged",
        "no_repo",
        "issue_closed",
        "issue_deleted",
        "no_longer_targeted",
        "superseded_closure",
    }
)
"""Reasons durable issue/notification work is no longer actionable."""


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
    HUMAN_READING = "human_reading"
    UNATTENDED_COPY_MODE = "unattended_copy_mode"
    SETTLING = "settling"
    DELIVERY_FAILED = "delivery_failed"
    EXPIRED = "expired"
    """A queued message dropped after ``timing.queue_expiry_minutes`` — terminal, never retried."""


BRIEF_SOURCE = "agent-brief"
"""The queue source of a runtime's startup brief (``brief_mode = "message"``):
delivered before anything else the session is sent."""

SUBSCRIPTION_KIND = "subscription"
"""The delivery kind of a batch of subscribed source events (``docs/sources.md``):
one message per agent and priority, grown while the agent is not ready."""


SUCCESS_OUTCOMES = frozenset({DeliveryOutcome.DELIVERED, DeliveryOutcome.RETRIED})
"""The message reached the agent."""
BLOCKED_OUTCOMES = frozenset(
    {
        DeliveryOutcome.OFFLINE,
        DeliveryOutcome.WAITING_FOR_HUMAN,
        DeliveryOutcome.AGENT_WORKING,
        DeliveryOutcome.HUMAN_TYPING,
        DeliveryOutcome.HUMAN_READING,
        DeliveryOutcome.UNATTENDED_COPY_MODE,
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
    sub_issues_total: int | None = None
    """GitHub's count of its sub-issues (``sub_issues_summary.total``); ``None`` when not given."""

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
    action: str = ""
    """GitHub's action (``opened``, ``reopened``, …); one event type covers several."""

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
            action=action,
        )


# Current plans are short authored snapshots, never runtime approval dialogs.
CURRENT_PLAN_BODY_BYTES = 65_536
PlanIdentity = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class PlanReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["issue", "sheet", "document"]
    target: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def valid_target(self):
        patterns = {
            "issue": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*",
            "sheet": r"[0-9a-f]{32}",
            "document": r"[1-9][0-9]*",
        }
        if not re.fullmatch(patterns[self.kind], self.target):
            raise ValueError("reference target does not match its kind")
        return self


class CurrentPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    item_id: PlanIdentity
    text: ReportText = Field(min_length=1, max_length=240)
    section: Literal["now", "next", "later"]
    status: Literal["pending", "in_progress", "blocked", "done"]
    detail: str = Field(default="", max_length=400)
    references: list[PlanReference] = Field(default_factory=list, max_length=3)

    @field_validator("detail")
    @classmethod
    def plain_detail(cls, value):
        return _report_text(value) if value else value


class CurrentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_id: PlanIdentity
    title: ReportText = Field(min_length=1, max_length=160)
    items: list[CurrentPlanItem] = Field(max_length=30)

    @model_validator(mode="after")
    def bounded_ordered_items(self):
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("item IDs must be unique")
        sections = [{"now": 0, "next": 1, "later": 2}[i.section] for i in self.items]
        if sections != sorted(sections):
            raise ValueError("order items by now, next, then later")
        size = len(self.title) + sum(
            len(i.text) + len(i.detail) + sum(len(r.target) for r in i.references)
            for i in self.items
        )
        if size > 8000:
            raise ValueError("current plan exceeds 8000 combined text characters")
        return self


class PublishCurrentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    author: ReportAgentName
    author_id: PlanIdentity
    request_id: PlanIdentity
    base_revision_id: PlanIdentity | None
    plan: CurrentPlan


class CurrentPlanFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    author: ReportAgentName | None
    request_id: PlanIdentity
    observed_plan_id: PlanIdentity
    observed_revision_id: PlanIdentity
    item_ids: list[PlanIdentity] = Field(default_factory=list, max_length=10)
    kind: Literal["question", "feedback"]
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def feedback_text(cls, value):
        # Keep ordinary paragraph breaks, without permitting terminal control sequences.
        if not value.strip() or any(
            (unicodedata.category(c) in {"Cc", "Cs"} and c not in "\n\t")
            or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
            for c in value
        ):
            raise ValueError("feedback must be nonblank text without control or bidi characters")
        return value

    @field_validator("item_ids")
    @classmethod
    def unique_items(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("selected item IDs must be unique")
        return value


class SetupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["initialize", "first_agent", "cancel_first_agent", "validate"]
    status: Literal["completed", "action_required", "failed"]
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(max_length=2000)
    data: dict
    completed_steps: list[str] = Field(max_length=30)
    retry: Literal["same_request", "new_request", "none"]


def setup_result(
    action, code, message, *, status="action_required", data=None, steps=(), retry=None
):
    return {
        "action": action,
        "status": status,
        "code": code,
        "message": message,
        "data": data or {},
        "completed_steps": list(steps),
        "retry": retry
        or {
            "invalid_request": "new_request",
            "request_conflict": "new_request",
            "agent_conflict": "new_request",
            "launch_failed": "none",
        }.get(code, "same_request"),
    }
