"""Issue endpoints — GitHub issue proxy with routing enrichment."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.api.deps import get_config, get_db, get_delivery_service, get_github
from agent_backbone.api.models import (
    IssueCommentRequest,
    IssueCommentResponse,
    IssueCreateRequest,
    IssueDependencies,
    IssueResponse,
    IssueUpdateRequest,
    ListEnvelope,
    ParsedLabelsResponse,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.models import parse_from_tag
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.routing import DeliveryService
from agent_backbone.services.routing._resolution import validate_issue_targets

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["issues"])


def _issue_to_response(
    issue, config: BackboneConfig, delivery_svc: DeliveryService, dependents: int = 0
) -> IssueResponse:
    score = delivery_svc.compute_priority_score(issue, config.priority_scoring, dependents)
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


def _comment_to_response(comment) -> IssueCommentResponse:
    return IssueCommentResponse(
        id=comment.id,
        body=comment.body,
        user_login=comment.user_login,
        from_entity=parse_from_tag(comment.body) if comment.body else None,
    )


@router.get("/issues", response_model=ListEnvelope[IssueResponse])
async def list_issues(
    state: str = Query(default="open"),
    for_entity: str | None = Query(default=None, alias="for"),
    from_entity: str | None = Query(default=None, alias="from"),
    issue_type: str | None = Query(default=None, alias="type"),
    label: str | None = Query(default=None),
    repo: str = Query(..., description="owner/name"),
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """List issues in a repository with filtering. Enriched with priority scores."""
    labels: list[str] = []
    if for_entity:
        labels.append(f"for:{for_entity}")
    if from_entity:
        labels.append(f"from:{from_entity}")
    if issue_type:
        labels.append(issue_type)
    if label:
        labels.append(label)

    issues = await gh.list_issues(state=state, labels=labels, repo_full_name=repo)
    items = [_issue_to_response(i, config, delivery_svc) for i in issues]
    return ListEnvelope(items=items, total=len(items))


@router.get("/issues/{number}", response_model=IssueResponse)
async def get_issue(
    number: int,
    repo: str = Query(..., description="owner/name"),
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Get a single issue by number with priority score."""
    try:
        issue = await gh.get_issue(number, repo_full_name=repo)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Issue #{number} not found") from e
    return _issue_to_response(issue, config, delivery_svc)


@router.get("/issues/{number}/comments", response_model=ListEnvelope[IssueCommentResponse])
async def list_issue_comments(
    number: int,
    repo: str = Query(..., description="owner/name"),
    gh: GitHubClient = Depends(get_github),
):
    """List comments on an issue with parsed [from:X] tags."""
    comments = await gh.list_comments(number, repo_full_name=repo)
    items = [_comment_to_response(c) for c in comments]
    return ListEnvelope(items=items, total=len(items))


@router.get("/issues/{number}/dependencies", response_model=IssueDependencies)
async def get_issue_dependencies(
    number: int,
    repo: str = Query(..., description="owner/name"),
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Get sub-issues and parent issues for an issue."""
    sub_issues = await gh.get_sub_issues(number, repo_full_name=repo)
    parents = await db.get_parents(number, repo=repo)
    return IssueDependencies(
        sub_issues=[_issue_to_response(s, config, delivery_svc) for s in sub_issues],
        parents=parents,
    )


@router.post("/issues", response_model=IssueResponse, status_code=201)
async def create_issue(
    body: IssueCreateRequest,
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Create an issue and immediately notify its ``for:`` targets."""
    if not body.title:
        raise HTTPException(status_code=400, detail="title is required")
    targets = [label.removeprefix("for:") for label in body.labels if label.startswith("for:")]
    try:
        validate_issue_targets(targets, config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    issue = await delivery_svc.create_and_notify(
        gh,
        body.title,
        body.body,
        body.labels,
        config,
        repo=body.repo,
        db=db,
        flow_name="api-create-issue",
    )
    return _issue_to_response(issue, config, delivery_svc)


@router.post("/issues/{number}/comment", response_model=IssueCommentResponse)
async def add_issue_comment(
    number: int,
    body: IssueCommentRequest,
    repo: str = Query(..., description="owner/name"),
    gh: GitHubClient = Depends(get_github),
):
    """Add a comment to an issue."""
    if not body.body:
        raise HTTPException(status_code=400, detail="body is required")
    comment = await gh.add_comment(number, body.body, repo_full_name=repo)
    return _comment_to_response(comment)


@router.patch("/issues/{number}", response_model=IssueResponse)
async def update_issue(
    number: int,
    body: IssueUpdateRequest,
    repo: str = Query(..., description="owner/name"),
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Update an issue (e.g. close it)."""
    if body.state not in ("open", "closed"):
        raise HTTPException(status_code=400, detail="state must be 'open' or 'closed'")
    issue = await gh.update_issue(number, body.state, repo_full_name=repo)
    return _issue_to_response(issue, config, delivery_svc)
