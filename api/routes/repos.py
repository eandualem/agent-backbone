"""Repository onboarding endpoints — discovery, status, and automated setup."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from api.models import (
    CheckDetail,
    ListEnvelope,
    OnboardingStepDetail,
    RepoOnboardRequest,
    RepoOnboardResponse,
    RepoStatusResponse,
)
from src.onboarding import (
    discover_repos,
    run_onboarding,
    run_status_checks,
    validate_org,
    validate_repo_name,
)

router = APIRouter(prefix="/api", tags=["repos"])


def _status_to_response(status) -> RepoStatusResponse:
    """Convert internal RepoStatus to API response model."""
    return RepoStatusResponse(
        org=status.org,
        repo=status.repo,
        onboarded=status.onboarded,
        checks=[
            CheckDetail(
                check=c.check,
                name=c.name,
                status=c.status,
                path=c.path,
                detail=c.detail,
            )
            for c in status.checks
        ],
    )


@router.get("/repos", response_model=ListEnvelope[RepoStatusResponse])
async def list_repos():
    """List all discovered repos with their onboarding status."""
    repos = discover_repos()
    items = []
    for entry in repos:
        status = run_status_checks(entry.org, entry.repo)
        items.append(_status_to_response(status))
    return ListEnvelope(items=items, total=len(items))


@router.post("/repos/onboard", response_model=RepoOnboardResponse, status_code=201)
async def onboard_repo(body: RepoOnboardRequest, request: Request):
    """Onboard a new repository — runs automated setup steps."""
    if not validate_org(body.org):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown org: {body.org}."
            " Known: Arclio, WF, Loveble, Tenacious",
        )

    backbone_config = getattr(request.app.state, "config", None)
    result = await run_onboarding(body.org, body.url, config=backbone_config)
    return RepoOnboardResponse(
        org=result.org,
        repo=result.repo,
        success=result.success,
        error=result.error,
        steps=[
            OnboardingStepDetail(
                step=s.step,
                name=s.name,
                status=s.status,
                detail=s.detail,
                command=s.command,
            )
            for s in result.steps
        ],
    )


@router.get("/repos/{org}/{repo}/status", response_model=RepoStatusResponse)
async def get_repo_status(org: str, repo: str):
    """Get onboarding status for a specific repo."""
    if not validate_org(org):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown org: {org}."
            " Known: Arclio, WF, Loveble, Tenacious",
        )
    if not validate_repo_name(repo):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid repo name: {repo}."
            " Must match [a-zA-Z0-9_-]+",
        )

    status = run_status_checks(org, repo)
    return _status_to_response(status)
