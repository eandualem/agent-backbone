"""Repository / onboarding API models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckDetail(BaseModel):
    """Single onboarding status check result."""

    check: int  # 1-7
    name: str
    status: str  # "ok" | "missing" | "info"
    path: str = ""
    detail: str = ""


class RepoStatusResponse(BaseModel):
    """Repository onboarding status with individual checks."""

    org: str
    repo: str
    onboarded: bool = False
    checks: list[CheckDetail] = Field(default_factory=list)


class RepoOnboardRequest(BaseModel):
    """Request body for onboarding a new repository."""

    org: str
    url: str  # SSH URL, e.g. git@github.com:eandualem/repo.git


class OnboardingStepDetail(BaseModel):
    """Single onboarding step result."""

    step: int
    name: str
    status: str  # "done" | "skipped" | "failed" | "manual_required"
    detail: str = ""
    command: str | None = None


class RepoOnboardResponse(BaseModel):
    """Onboarding execution result."""

    org: str
    repo: str
    success: bool = False
    error: str = ""
    steps: list[OnboardingStepDetail] = Field(default_factory=list)
