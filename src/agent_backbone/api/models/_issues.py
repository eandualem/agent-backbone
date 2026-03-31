"""Issue-related API models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ParsedLabelsResponse(BaseModel):
    """Label breakdown for an issue."""

    sender: str = "unknown"
    targets: list[str] = Field(default_factory=list)
    issue_type: str = ""
    priority: str = ""
    all_labels: list[str] = Field(default_factory=list)


class IssueSummary(BaseModel):
    """Summary projection for one canonical issue."""

    repo_full_name: str = ""
    number: int
    title: str = ""
    state: str = "open"
    html_url: str = ""
    labels: ParsedLabelsResponse = Field(default_factory=ParsedLabelsResponse)
    priority_score: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    comment_count: int = 0
    user_login: str = "unknown"


class IssueDetail(IssueSummary):
    """Detail projection for one canonical issue."""

    body: str = ""


class IssueResponse(IssueSummary):
    """Backward-compatible alias for summary issue payloads."""


class IssueCommentResponse(BaseModel):
    """Issue comment with parsed from-tag."""

    id: int = 0
    body: str = ""
    user_login: str = "unknown"
    from_entity: str | None = None
    created_at: str = ""
    updated_at: str = ""


class IssueDependencies(BaseModel):
    """Sub-issues and parent issues for an issue."""

    sub_issues: list[IssueSummary] = Field(default_factory=list)
    parents: list[IssueSummary] = Field(default_factory=list)
