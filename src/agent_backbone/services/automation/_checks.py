"""Onboarding status checks — 7 filesystem checks for repo onboarding state.

Each check returns a CheckResult indicating whether a particular onboarding
step has been completed (ok/missing/info).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import agent_backbone.services.automation._pipeline as _pl

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    check: int
    name: str
    status: str  # "ok" | "missing" | "info"
    path: str = ""
    detail: str = ""


def _check_spec_dir(org: str, repo: str) -> CheckResult:
    """Check 1: spec directory exists."""
    path = _pl._SPEC_ROOT / org / repo / "docs"
    return CheckResult(
        check=1,
        name="spec_directory",
        status="ok" if path.is_dir() else "missing",
        path=str(path),
    )


def _check_orch_config(org: str, repo: str) -> CheckResult:
    """Check 2: orchestration CLAUDE.md exists."""
    path = _pl._ORCH_ROOT / org / repo / "CLAUDE.md"
    return CheckResult(
        check=2,
        name="orchestration_config",
        status="ok" if path.is_file() else "missing",
        path=str(path),
    )


def _check_registry_entries(org: str, repo: str) -> CheckResult:
    """Check 3: repo has entries in symlinks.conf."""
    target = f"core/code/{org}/{repo}/"
    if not _pl._REGISTRY_PATH.is_file():
        return CheckResult(
            check=3,
            name="registry_entries",
            status="missing",
            path=str(_pl._REGISTRY_PATH),
            detail="symlinks.conf not found",
        )
    content = _pl._REGISTRY_PATH.read_text()
    found = target in content
    return CheckResult(
        check=3,
        name="registry_entries",
        status="ok" if found else "missing",
        path=str(_pl._REGISTRY_PATH),
    )


def _check_symlinks_created(org: str, repo: str) -> CheckResult:
    """Check 4: CLAUDE.md in workspace is a symlink (setup.sh ran)."""
    path = _pl._WS_ROOT / org / repo / "CLAUDE.md"
    return CheckResult(
        check=4,
        name="symlinks_created",
        status="ok" if path.is_symlink() else "missing",
        path=str(path),
    )


def _check_sparse_checkout(org: str, repo: str) -> CheckResult:
    """Check 5: sparse-checkout file exists (SDD step)."""
    path = _pl._WS_ROOT / org / repo / ".git" / "info" / "sparse-checkout"
    return CheckResult(
        check=5,
        name="sparse_checkout",
        status="ok" if path.is_file() else "missing",
        path=str(path),
        detail="SDD step — requires sdd-init.sh" if not path.is_file() else "",
    )


def _check_git_excludes(org: str, repo: str) -> CheckResult:
    """Check 6: .git/info/exclude is a symlink (SDD step)."""
    path = _pl._WS_ROOT / org / repo / ".git" / "info" / "exclude"
    return CheckResult(
        check=6,
        name="git_excludes",
        status="ok" if path.is_symlink() else "missing",
        path=str(path),
        detail="SDD step — requires sdd-init.sh" if not path.is_symlink() else "",
    )


def _check_assume_unchanged(org: str, repo: str) -> CheckResult:
    """Check 7: sparse-checkout contains SDD pattern (proxy for assume-unchanged)."""
    path = _pl._WS_ROOT / org / repo / ".git" / "info" / "sparse-checkout"
    if not path.is_file():
        return CheckResult(
            check=7,
            name="assume_unchanged",
            status="missing",
            path=str(path),
            detail="No sparse-checkout — SDD not configured",
        )
    content = path.read_text()
    has_pattern = "!/docs/" in content and "!/CLAUDE.md" in content
    return CheckResult(
        check=7,
        name="assume_unchanged",
        status="ok" if has_pattern else "missing",
        path=str(path),
        detail="" if has_pattern else "SDD patterns not found in sparse-checkout",
    )


@dataclass
class RepoStatus:
    org: str
    repo: str
    onboarded: bool = False
    checks: list[CheckResult] = field(default_factory=list)


def run_status_checks(org: str, repo: str) -> RepoStatus:
    """Run all 7 status checks for a repo."""
    checks = [
        _check_spec_dir(org, repo),
        _check_orch_config(org, repo),
        _check_registry_entries(org, repo),
        _check_symlinks_created(org, repo),
        _check_sparse_checkout(org, repo),
        _check_git_excludes(org, repo),
        _check_assume_unchanged(org, repo),
    ]
    onboarded = all(c.status == "ok" for c in checks)
    return RepoStatus(org=org, repo=repo, onboarded=onboarded, checks=checks)
