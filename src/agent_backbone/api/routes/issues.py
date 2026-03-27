"""Issue management endpoints — GitHub issue proxy with enrichment."""

from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.api.deps import get_config, get_db, get_github
from agent_backbone.api.models import (
    IssueCommentResponse,
    IssueDependencies,
    IssueResponse,
    ListEnvelope,
    ParsedLabelsResponse,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.models import parse_from_tag
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient, GitHubServiceError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["issues"])


def _issue_to_response(
    issue, config: BackboneConfig, dependents: int = 0
) -> IssueResponse:
    """Convert IssueData to IssueResponse with priority score."""
    score = 0.0
    return IssueResponse(
        number=issue.number,
        title=issue.title,
        state=issue.state,
        html_url=issue.html_url,
        repo_full_name=issue.repo_full_name,
        labels=ParsedLabelsResponse(
            sender=issue.labels.sender,
            targets=issue.labels.targets,
            issue_type=issue.labels.issue_type,
            priority=issue.labels.priority,
        ),
        priority_score=score,
    )


def _raise_github_http_error(
    exc: GitHubServiceError,
    *,
    not_found_detail: str | None = None,
) -> None:
    if exc.status_code == 404:
        raise HTTPException(status_code=404, detail=not_found_detail or exc.message) from exc

    headers: dict[str, str] = {}
    if exc.retry_after_seconds and exc.retry_after_seconds > 0:
        headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after_seconds)))
    status_code = 503 if exc.retry_allowed else 502
    raise HTTPException(
        status_code=status_code,
        detail=exc.message,
        headers=headers or None,
    ) from exc


@router.get("/issues", response_model=ListEnvelope[IssueResponse])
async def list_issues(
    state: str = Query(default="open"),
    for_entity: str | None = Query(default=None),
    from_entity: str | None = Query(default=None),
    issue_type: str | None = Query(default=None, alias="type"),
    label: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
):
    """List issues with filtering. Enriched with priority scores."""
    labels: list[str] = []
    if for_entity:
        labels.append(f"for:{for_entity}")
    if from_entity:
        labels.append(f"from:{from_entity}")
    if issue_type:
        labels.append(issue_type)
    if label:
        labels.append(label)

    try:
        issues = await gh.list_issues(state=state, labels=labels, repo_full_name=repo)
    except GitHubServiceError as exc:
        _raise_github_http_error(exc)
    items = [_issue_to_response(i, config) for i in issues]
    return ListEnvelope(items=items, total=len(items))


@router.get("/issues/{number}", response_model=IssueResponse)
async def get_issue(
    number: int,
    repo: str | None = Query(default=None),
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
):
    """Get a single issue by number with priority score."""
    try:
        issue = await gh.get_issue(number, repo_full_name=repo)
    except GitHubServiceError as exc:
        _raise_github_http_error(exc, not_found_detail=f"Issue #{number} not found")
    return _issue_to_response(issue, config)


@router.get("/issues/{number}/comments", response_model=ListEnvelope[IssueCommentResponse])
async def list_issue_comments(
    number: int,
    repo: str | None = Query(default=None),
    gh: GitHubClient = Depends(get_github),
):
    """List comments on an issue with parsed [from:X] tags."""
    try:
        comments = await gh.list_comments(number, repo_full_name=repo)
    except GitHubServiceError as exc:
        _raise_github_http_error(exc, not_found_detail=f"Issue #{number} not found")
    items = [
        IssueCommentResponse(
            id=c.id,
            body=c.body,
            user_login=c.user_login,
            from_entity=parse_from_tag(c.body) if c.body else None,
        )
        for c in comments
    ]
    return ListEnvelope(items=items, total=len(items))


@router.get("/issues/{number}/dependencies", response_model=IssueDependencies)
async def get_issue_dependencies(
    number: int,
    repo: str | None = Query(default=None),
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    db: BackboneDB = Depends(get_db),
):
    """Get sub-issues and parent issues for an issue."""
    sub_issues = await gh.get_sub_issues(number, repo_full_name=repo)
    parents = await db.get_parents(number)
    return IssueDependencies(
        sub_issues=[_issue_to_response(s, config) for s in sub_issues],
        parents=parents,
    )


@router.post("/issues", response_model=IssueResponse)
async def create_issue(
    body: dict,
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    db: BackboneDB = Depends(get_db),
):
    """Create a new issue in the orchestration repo."""
    title = body.get("title", "")
    issue_body = body.get("body", "")
    labels = body.get("labels", [])
    repo_full_name = body.get("repo") or body.get("repo_full_name")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    issue = await gh.create_issue(
        title,
        issue_body,
        labels,
        repo_full_name=repo_full_name,
    )
    return _issue_to_response(issue, config)


@router.post("/issues/{number}/comment", response_model=IssueCommentResponse)
async def add_issue_comment(
    number: int,
    body: dict,
    gh: GitHubClient = Depends(get_github),
):
    """Add a comment to an issue."""
    comment_body = body.get("body", "")
    repo_full_name = body.get("repo") or body.get("repo_full_name")
    if not comment_body:
        raise HTTPException(status_code=400, detail="body is required")
    try:
        comment = await gh.add_comment(number, comment_body, repo_full_name=repo_full_name)
    except GitHubServiceError as exc:
        _raise_github_http_error(exc, not_found_detail=f"Issue #{number} not found")
    return IssueCommentResponse(
        id=comment.id,
        body=comment.body,
        user_login=comment.user_login,
        from_entity=parse_from_tag(comment.body) if comment.body else None,
    )


@router.patch("/issues/{number}", response_model=IssueResponse)
async def update_issue(
    number: int,
    body: dict,
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
):
    """Update an issue (e.g. close it)."""
    state = body.get("state")
    repo_full_name = body.get("repo") or body.get("repo_full_name")
    if not state:
        raise HTTPException(status_code=400, detail="state is required")
    try:
        issue = await gh.update_issue(number, state, repo_full_name=repo_full_name)
    except GitHubServiceError as exc:
        _raise_github_http_error(exc, not_found_detail=f"Issue #{number} not found")
    return _issue_to_response(issue, config)
