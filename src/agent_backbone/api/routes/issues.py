"""Issue management endpoints — GitHub issue proxy with enrichment."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.api.deps import get_config, get_db, get_delivery_service, get_github
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
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.routing import DeliveryService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["issues"])


def _issue_to_response(
    issue, config: BackboneConfig, delivery_svc: DeliveryService, dependents: int = 0
) -> IssueResponse:
    """Convert IssueData to IssueResponse with priority score."""
    score = delivery_svc.compute_priority_score(issue, config.priority_scoring, dependents)
    return IssueResponse(
        number=issue.number,
        title=issue.title,
        state=issue.state,
        html_url=issue.html_url,
        labels=ParsedLabelsResponse(
            sender=issue.labels.sender,
            targets=issue.labels.targets,
            issue_type=issue.labels.issue_type,
            priority=issue.labels.priority,
        ),
        priority_score=score,
    )


@router.get("/issues", response_model=ListEnvelope[IssueResponse])
async def list_issues(
    state: str = Query(default="open"),
    for_entity: str | None = Query(default=None),
    from_entity: str | None = Query(default=None),
    issue_type: str | None = Query(default=None, alias="type"),
    label: str | None = Query(default=None),
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
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

    issues = await gh.list_issues(state=state, labels=labels)
    items = [_issue_to_response(i, config, delivery_svc) for i in issues]
    return ListEnvelope(items=items, total=len(items))


@router.get("/issues/{number}", response_model=IssueResponse)
async def get_issue(
    number: int,
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Get a single issue by number with priority score."""
    try:
        issue = await gh.get_issue(number)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Issue #{number} not found") from e
    return _issue_to_response(issue, config, delivery_svc)


@router.get("/issues/{number}/comments", response_model=ListEnvelope[IssueCommentResponse])
async def list_issue_comments(
    number: int,
    gh: GitHubClient = Depends(get_github),
):
    """List comments on an issue with parsed [from:X] tags."""
    comments = await gh.list_comments(number)
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
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Get sub-issues and parent issues for an issue."""
    sub_issues = await gh.get_sub_issues(number)
    parents = await db.get_parents(number)
    return IssueDependencies(
        sub_issues=[_issue_to_response(s, config, delivery_svc) for s in sub_issues],
        parents=parents,
    )


@router.post("/issues", response_model=IssueResponse)
async def create_issue(
    body: dict,
    config: BackboneConfig = Depends(get_config),
    gh: GitHubClient = Depends(get_github),
    db: BackboneDB = Depends(get_db),
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Create a new issue in the orchestration repo."""
    title = body.get("title", "")
    issue_body = body.get("body", "")
    labels = body.get("labels", [])
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    issue = await delivery_svc.create_and_notify(
        gh,
        title,
        issue_body,
        labels,
        config,
        db=db,
        flow_name="api-create-issue",
    )
    return _issue_to_response(issue, config, delivery_svc)


@router.post("/issues/{number}/comment", response_model=IssueCommentResponse)
async def add_issue_comment(
    number: int,
    body: dict,
    gh: GitHubClient = Depends(get_github),
):
    """Add a comment to an issue."""
    comment_body = body.get("body", "")
    if not comment_body:
        raise HTTPException(status_code=400, detail="body is required")
    comment = await gh.add_comment(number, comment_body)
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
    delivery_svc: DeliveryService = Depends(get_delivery_service),
):
    """Update an issue (e.g. close it)."""
    state = body.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="state is required")
    issue = await gh.update_issue(number, state)
    return _issue_to_response(issue, config, delivery_svc)
