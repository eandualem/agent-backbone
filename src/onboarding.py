"""Repo onboarding — discovery, status checks, and automated setup.

Scans the workspace for repositories, checks their onboarding status
(7 checks), and executes the 6 automatable onboarding steps for new repos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known organisations (filesystem convention under ~/ws/core/code/)
# ---------------------------------------------------------------------------

KNOWN_ORGS = ("Arclio", "WF", "Loveble", "Tenacious")

# ---------------------------------------------------------------------------
# Path roots — module-level for easy patching in tests
# ---------------------------------------------------------------------------

_WS_ROOT = Path("~/ws/core/code").expanduser()
_SPEC_ROOT = Path("~/ws/core/spec").expanduser()
_ORCH_ROOT = Path("~/orchestration/core/code").expanduser()
_REGISTRY_PATH = Path("~/infra/registry/symlinks.conf").expanduser()
_SETUP_SCRIPT = Path("~/infra/scripts/setup.sh").expanduser()
_REPOS_JSON = Path("~/.claude/state/repos.json").expanduser()

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_org(org: str) -> bool:
    """Check org is in the known list."""
    return org in KNOWN_ORGS


def validate_repo_name(repo: str) -> bool:
    """Check repo name is safe (alphanumeric, hyphens, underscores)."""
    return bool(_REPO_NAME_RE.match(repo))


# ---------------------------------------------------------------------------
# Registry (repos.json)
# ---------------------------------------------------------------------------


@dataclass
class RepoEntry:
    org: str
    repo: str


def load_repos_json() -> list[RepoEntry]:
    """Load the mutable repo registry."""
    if not _REPOS_JSON.is_file():
        return []
    try:
        data = json.loads(_REPOS_JSON.read_text())
        return [RepoEntry(org=r["org"], repo=r["repo"]) for r in data]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("Corrupt repos.json — returning empty list")
        return []


def save_repos_json(entries: list[RepoEntry]) -> None:
    """Write the repo registry atomically."""
    _REPOS_JSON.parent.mkdir(parents=True, exist_ok=True)
    data = [{"org": e.org, "repo": e.repo} for e in entries]
    _REPOS_JSON.write_text(json.dumps(data, indent=2) + "\n")


def register_repo(org: str, repo: str) -> None:
    """Add a repo to repos.json if not already present."""
    entries = load_repos_json()
    for e in entries:
        if e.org == org and e.repo == repo:
            return  # already registered
    entries.append(RepoEntry(org=org, repo=repo))
    save_repos_json(entries)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_repos() -> list[RepoEntry]:
    """Scan filesystem for repos in known orgs, merge with repos.json."""
    seen: set[tuple[str, str]] = set()
    result: list[RepoEntry] = []

    # Scan filesystem
    for org in KNOWN_ORGS:
        org_dir = _WS_ROOT / org
        if not org_dir.is_dir():
            continue
        for child in sorted(org_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                key = (org, child.name)
                if key not in seen:
                    seen.add(key)
                    result.append(RepoEntry(org=org, repo=child.name))

    # Merge repos.json entries not on disk
    for entry in load_repos_json():
        key = (entry.org, entry.repo)
        if key not in seen:
            seen.add(key)
            result.append(entry)

    return result


# ---------------------------------------------------------------------------
# Status checks (7 checks, all sync filesystem)
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    check: int
    name: str
    status: str  # "ok" | "missing" | "info"
    path: str = ""
    detail: str = ""


def _check_spec_dir(org: str, repo: str) -> CheckResult:
    """Check 1: spec directory exists."""
    path = _SPEC_ROOT / org / repo / "docs"
    return CheckResult(
        check=1,
        name="spec_directory",
        status="ok" if path.is_dir() else "missing",
        path=str(path),
    )


def _check_orch_config(org: str, repo: str) -> CheckResult:
    """Check 2: orchestration CLAUDE.md exists."""
    path = _ORCH_ROOT / org / repo / "CLAUDE.md"
    return CheckResult(
        check=2,
        name="orchestration_config",
        status="ok" if path.is_file() else "missing",
        path=str(path),
    )


def _check_registry_entries(org: str, repo: str) -> CheckResult:
    """Check 3: repo has entries in symlinks.conf."""
    target = f"core/code/{org}/{repo}/"
    if not _REGISTRY_PATH.is_file():
        return CheckResult(
            check=3,
            name="registry_entries",
            status="missing",
            path=str(_REGISTRY_PATH),
            detail="symlinks.conf not found",
        )
    content = _REGISTRY_PATH.read_text()
    found = target in content
    return CheckResult(
        check=3,
        name="registry_entries",
        status="ok" if found else "missing",
        path=str(_REGISTRY_PATH),
    )


def _check_symlinks_created(org: str, repo: str) -> CheckResult:
    """Check 4: CLAUDE.md in workspace is a symlink (setup.sh ran)."""
    path = _WS_ROOT / org / repo / "CLAUDE.md"
    return CheckResult(
        check=4,
        name="symlinks_created",
        status="ok" if path.is_symlink() else "missing",
        path=str(path),
    )


def _check_sparse_checkout(org: str, repo: str) -> CheckResult:
    """Check 5: sparse-checkout file exists (SDD step)."""
    path = _WS_ROOT / org / repo / ".git" / "info" / "sparse-checkout"
    return CheckResult(
        check=5,
        name="sparse_checkout",
        status="ok" if path.is_file() else "missing",
        path=str(path),
        detail="SDD step — requires sdd-init.sh" if not path.is_file() else "",
    )


def _check_git_excludes(org: str, repo: str) -> CheckResult:
    """Check 6: .git/info/exclude is a symlink (SDD step)."""
    path = _WS_ROOT / org / repo / ".git" / "info" / "exclude"
    return CheckResult(
        check=6,
        name="git_excludes",
        status="ok" if path.is_symlink() else "missing",
        path=str(path),
        detail="SDD step — requires sdd-init.sh" if not path.is_symlink() else "",
    )


def _check_assume_unchanged(org: str, repo: str) -> CheckResult:
    """Check 7: sparse-checkout contains SDD pattern (proxy for assume-unchanged)."""
    path = _WS_ROOT / org / repo / ".git" / "info" / "sparse-checkout"
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


# ---------------------------------------------------------------------------
# symlinks.conf block generation
# ---------------------------------------------------------------------------


def _symlink_block(org: str, repo: str) -> str:
    """Generate the symlinks.conf entries for a repo."""
    # Relative target depth: core/code/{org}/{repo}/X -> ../../../../../orchestration/core/code/{org}/{repo}/X
    t = f"../../../../../orchestration/core/code/{org}/{repo}"
    lines = [
        f"agent-repo | core/code/{org}/{repo}/CLAUDE.md | {t}/CLAUDE.md | {repo} identity",
        f"agent-repo | core/code/{org}/{repo}/.claude | {t}/.claude | {repo} agents",
        f"agent-repo | core/code/{org}/{repo}/.cursor | {t}/.cursor | {repo} Cursor",
        f"agent-repo | core/code/{org}/{repo}/.gemini | {t}/.gemini | {repo} Gemini",
        f"spec-docs | core/code/{org}/{repo}/docs | ../../../spec/{org}/{repo}/docs | sdd-init.sh",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration templates
# ---------------------------------------------------------------------------

_CLAUDE_MD_TEMPLATE = "@AGENTS.md\n\n# {repo}\n\n> Repo-specific context for Claude Code agents working in this repository.\n"

_AGENTS_MD_TEMPLATE = "@../../AGENTS.md\n"

_SETTINGS_LOCAL_JSON = '{{"additionalDirectories": []}}\n'


# ---------------------------------------------------------------------------
# Onboarding execution
# ---------------------------------------------------------------------------


@dataclass
class OnboardingStep:
    step: int
    name: str
    status: str  # "done" | "skipped" | "failed" | "manual_required"
    detail: str = ""
    command: str | None = None


@dataclass
class OnboardingResult:
    org: str
    repo: str
    success: bool = False
    error: str = ""
    steps: list[OnboardingStep] = field(default_factory=list)


async def run_onboarding(org: str, repo: str) -> OnboardingResult:
    """Execute automated onboarding steps for a new repo."""
    steps: list[OnboardingStep] = []
    had_failure = False

    # Step 1: Create spec directory
    spec_dir = _SPEC_ROOT / org / repo / "docs" / "specifications"
    try:
        spec_dir.mkdir(parents=True, exist_ok=True)
        steps.append(OnboardingStep(step=1, name="spec_directory", status="done", detail=str(spec_dir)))
    except OSError as exc:
        steps.append(OnboardingStep(step=1, name="spec_directory", status="failed", detail=str(exc)))
        had_failure = True

    # Step 2: Create orchestration config
    orch_dir = _ORCH_ROOT / org / repo
    claude_dir = orch_dir / ".claude"
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)

        claude_md = orch_dir / "CLAUDE.md"
        if not claude_md.exists():
            claude_md.write_text(_CLAUDE_MD_TEMPLATE.format(repo=repo))

        agents_md = orch_dir / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text(_AGENTS_MD_TEMPLATE)

        settings_json = claude_dir / "settings.local.json"
        if not settings_json.exists():
            settings_json.write_text(_SETTINGS_LOCAL_JSON)

        steps.append(OnboardingStep(step=2, name="orchestration_config", status="done", detail=str(orch_dir)))
    except OSError as exc:
        steps.append(OnboardingStep(step=2, name="orchestration_config", status="failed", detail=str(exc)))
        had_failure = True

    # Step 3: Append to symlinks.conf
    try:
        marker = f"core/code/{org}/{repo}/"
        content = _REGISTRY_PATH.read_text() if _REGISTRY_PATH.is_file() else ""
        if marker in content:
            steps.append(OnboardingStep(step=3, name="registry_entries", status="skipped", detail="Already in symlinks.conf"))
        else:
            block = _symlink_block(org, repo)
            # Ensure trailing newline before appending
            sep = "\n" if content and not content.endswith("\n") else ""
            _REGISTRY_PATH.write_text(content + sep + "\n" + block + "\n")
            steps.append(OnboardingStep(step=3, name="registry_entries", status="done", detail=f"Added {5} entries"))
    except OSError as exc:
        steps.append(OnboardingStep(step=3, name="registry_entries", status="failed", detail=str(exc)))
        had_failure = True

    # Step 4: Run setup.sh
    try:
        proc = await asyncio.create_subprocess_exec(
            str(_SETUP_SCRIPT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            steps.append(OnboardingStep(step=4, name="setup_script", status="done", detail="setup.sh completed"))
        else:
            err_msg = stderr.decode().strip()[:200] if stderr else f"exit code {proc.returncode}"
            steps.append(OnboardingStep(step=4, name="setup_script", status="failed", detail=err_msg))
            had_failure = True
    except OSError as exc:
        steps.append(OnboardingStep(step=4, name="setup_script", status="failed", detail=str(exc)))
        had_failure = True

    # Step 5: SDD init — manual step
    sdd_cmd = f"~/infra/scripts/sdd-init.sh --org {org} {repo}"
    steps.append(OnboardingStep(
        step=5,
        name="sdd_init",
        status="manual_required",
        detail="Run SDD initialisation (sparse-checkout, assume-unchanged, exclude symlink)",
        command=sdd_cmd,
    ))

    # Step 6: Register in repos.json
    try:
        register_repo(org, repo)
        steps.append(OnboardingStep(step=6, name="repos_json", status="done", detail=str(_REPOS_JSON)))
    except OSError as exc:
        steps.append(OnboardingStep(step=6, name="repos_json", status="failed", detail=str(exc)))
        had_failure = True

    return OnboardingResult(
        org=org,
        repo=repo,
        success=not had_failure,
        steps=steps,
    )
