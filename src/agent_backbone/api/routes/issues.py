"""Canonical issue-domain endpoints backed by the persistent projection."""

from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.api.deps import get_config, get_db, get_github, get_issue_service
from agent_backbone.api.models import (
    IssueCommentResponse,
    IssueDependencies,
    IssueDetail,
    IssueSummary,
    ListEnvelope,
    ParsedLabelsResponse,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient, GitHubServiceError
from agent_backbone.services.issues import IssueService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["issues"])


def _summary_from_dict(data: dict) -> IssueSummary:
    return IssueSummary(
        repo_full_name=data.get("repo_full_name", ""),
        number=int(data.get("number", 0)),
        title=data.get("title", ""),
        state=data.get("state", "open"),
        html_url=data.get("html_url", ""),
        labels=ParsedLabelsResponse(
            sender=data.get("labels", {}).get("sender", "unknown"),
            targets=data.get("labels", {}).get("targets", []),
            issue_type=data.get("labels", {}).get("issue_type", ""),
            priority=data.get("labels", {}).get("priority", ""),
            all_labels=data.get("labels", {}).get("all_labels", []),
        ),
        priority_score=float(data.get("priority_score", 0.0)),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        comment_count=int(data.get("comment_count", 0)),
        user_login=data.get("user_login", "unknown"),
    )


def _detail_from_dict(data: dict) -> IssueDetail:
    return IssueDetail(
        **_summary_from_dict(data).model_dump(mode="python"),
        body=data.get("body", ""),
    )


def _comment_from_dict(data: dict) -> IssueCommentResponse:
    return IssueCommentResponse(
        id=int(data.get("id", 0)),
        body=data.get("body", ""),
        user_login=data.get("user_login", "unknown"),
        from_entity=data.get("from_entity"),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
    )


def _raise_github_http_error(
    exc: GitHubServiceError, *, not_found_detail: str | None = None
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


def _repo_full_name(owner: str, repo: str) -> str:
    return f"{owner}/{repo}"


def _validate_targets(labels: list[str], config: BackboneConfig) -> None:
    tracked = set(config.registry.tracked_sessions.keys())
    for label in labels:
        if not isinstance(label, str) or not label.startswith("for:"):
            continue
        target = label[4:]
        if target not in tracked:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid for: label target '{target}'"
                " — expected a concrete, routable target",
            )


@router.get("/issues", response_model=ListEnvelope[IssueSummary])
async def list_issues(
    state: str = Query(default="open"),
    repo: str | None = Query(default=None),
    for_entity: str | None = Query(default=None),
    from_entity: str | None = Query(default=None),
    issue_type: str | None = Query(default=None, alias="type"),
    label: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=200, ge=1, le=500),
    issue_service: IssueService = Depends(get_issue_service),
):
    """List issues from the canonical projection."""
    items, total = await issue_service.list_issues(
        state=state,
        repo_full_name=repo,
        for_entity=for_entity,
        from_entity=from_entity,
        issue_type=issue_type,
        label=label,
        page=page,
        per_page=per_page,
    )
    return ListEnvelope(items=[_summary_from_dict(item) for item in items], total=total)


@router.get("/issues/{owner}/{repo}/{number}", response_model=IssueDetail)
async def get_issue(
    owner: str,
    repo: str,
    number: int,
    issue_service: IssueService = Depends(get_issue_service),
):
    """Get a single issue detail by canonical identity."""
    issue = await issue_service.get_issue(_repo_full_name(owner, repo), number)
    if issue is None:
        full = _repo_full_name(owner, repo)
        raise HTTPException(
            status_code=404, detail=f"Issue {full}#{number} not found"
        )
    return _detail_from_dict(issue)


@router.get(
    "/issues/{owner}/{repo}/{number}/comments",
    response_model=ListEnvelope[IssueCommentResponse],
)
async def list_issue_comments(
    owner: str,
    repo: str,
    number: int,
    issue_service: IssueService = Depends(get_issue_service),
):
    """List comments from the canonical projection."""
    repo_full_name = _repo_full_name(owner, repo)
    if await issue_service.get_issue(repo_full_name, number) is None:
        raise HTTPException(status_code=404, detail=f"Issue {repo_full_name}#{number} not found")
    comments = await issue_service.list_comments(repo_full_name, number)
    return ListEnvelope(
        items=[_comment_from_dict(comment) for comment in comments],
        total=len(comments),
    )


@router.get("/issues/{owner}/{repo}/{number}/dependencies", response_model=IssueDependencies)
async def get_issue_dependencies(
    owner: str,
    repo: str,
    number: int,
    issue_service: IssueService = Depends(get_issue_service),
    gh: GitHubClient = Depends(get_github),
    db: BackboneDB = Depends(get_db),
):
    """Get sub-issues and parent issues for an issue."""
    repo_full_name = _repo_full_name(owner, repo)
    if await issue_service.get_issue(repo_full_name, number) is None:
        raise HTTPException(status_code=404, detail=f"Issue {repo_full_name}#{number} not found")
    sub_issues = await gh.get_sub_issues(number, repo_full_name=repo_full_name)
    sub_issue_items: list[IssueSummary] = []
    for sub_issue in sub_issues:
        detail = await issue_service.get_issue(repo_full_name, sub_issue.number)
        if detail is None:
            detail = await issue_service.refresh_issue(
                repo_full_name,
                sub_issue.number,
                issue_data=sub_issue,
                emit_changes=False,
            )
        sub_issue_items.append(_summary_from_dict(detail))

    parent_numbers = await db.get_parents(number)
    parents = await db.issues.get_issues_by_numbers(repo_full_name, parent_numbers)
    return IssueDependencies(
        sub_issues=sub_issue_items,
        parents=[_summary_from_dict(parent) for parent in parents],
    )


@router.post("/issues", response_model=IssueSummary)
async def create_issue(
    body: dict,
    config: BackboneConfig = Depends(get_config),
    issue_service: IssueService = Depends(get_issue_service),
):
    """Create a new issue and refresh the canonical projection."""
    repo_full_name = body.get("repo_full_name") or body.get("repo")
    title = body.get("title", "")
    issue_body = body.get("body", "")
    labels = body.get("labels", [])
    if not repo_full_name:
        raise HTTPException(status_code=400, detail="repo_full_name is required")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    _validate_targets(labels, config)
    try:
        summary = await issue_service.create_issue(
            repo_full_name=repo_full_name,
            title=title,
            body=issue_body,
            labels=labels,
        )
    except GitHubServiceError as exc:
        _raise_github_http_error(exc)
    return _summary_from_dict(summary)


@router.post("/issues/{owner}/{repo}/{number}/comment", response_model=IssueCommentResponse)
async def add_issue_comment(
    owner: str,
    repo: str,
    number: int,
    body: dict,
    issue_service: IssueService = Depends(get_issue_service),
):
    """Add a comment and refresh the canonical projection."""
    comment_body = body.get("body", "")
    if not comment_body:
        raise HTTPException(status_code=400, detail="body is required")
    try:
        comment = await issue_service.add_comment(
            repo_full_name=_repo_full_name(owner, repo),
            issue_number=number,
            body=comment_body,
        )
    except GitHubServiceError as exc:
        _raise_github_http_error(
            exc,
            not_found_detail=f"Issue {_repo_full_name(owner, repo)}#{number} not found",
        )
    return _comment_from_dict(comment)


@router.patch("/issues/{owner}/{repo}/{number}", response_model=IssueSummary)
async def update_issue(
    owner: str,
    repo: str,
    number: int,
    body: dict,
    issue_service: IssueService = Depends(get_issue_service),
):
    """Update an issue and refresh the canonical projection."""
    state = body.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="state is required")
    try:
        issue = await issue_service.update_issue_state(
            repo_full_name=_repo_full_name(owner, repo),
            issue_number=number,
            state=state,
        )
    except GitHubServiceError as exc:
        _raise_github_http_error(
            exc,
            not_found_detail=f"Issue {_repo_full_name(owner, repo)}#{number} not found",
        )
    return _summary_from_dict(issue)
