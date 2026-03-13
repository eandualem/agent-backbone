"""Onboarding pipeline — discovery, validation, registry, and automated setup.

Contains the 10-step onboarding pipeline for new repos, filesystem discovery,
repo registry (repos.json) management, and validation utilities.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path roots — module-level for easy patching in tests
# ---------------------------------------------------------------------------

_WS_ROOT = Path("~/ws/core/code").expanduser()
_SPEC_ROOT = Path("~/ws/core/spec").expanduser()
_ORCH_ROOT = Path("~/orchestration/core/code").expanduser()
_REGISTRY_PATH = Path("~/infra/registry/symlinks.conf").expanduser()
_SETUP_SCRIPT = Path("~/infra/scripts/setup.sh").expanduser()
_SDD_INIT_SCRIPT = Path("~/infra/scripts/sdd-init.sh").expanduser()
_REPOS_JSON = Path("~/.claude/state/repos.json").expanduser()

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_SSH_URL_RE = re.compile(r"^git@[\w.-]+:[\w.-]+/([\w.-]+?)(?:\.git)?$")


def _discover_orgs() -> tuple[str, ...]:
    """Discover org directories under the workspace root."""
    if not _WS_ROOT.is_dir():
        return ()
    return tuple(
        sorted(d.name for d in _WS_ROOT.iterdir() if d.is_dir() and not d.name.startswith("."))
    )


def validate_org(org: str) -> bool:
    """Check org is in the known list."""
    return org in _discover_orgs()


def validate_repo_name(repo: str) -> bool:
    """Check repo name is safe (alphanumeric, hyphens, underscores)."""
    return bool(_REPO_NAME_RE.match(repo))


def parse_ssh_url(url: str) -> str | None:
    """Extract repo name from SSH URL. Returns None if invalid."""
    m = _SSH_URL_RE.match(url)
    return m.group(1) if m else None


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
    for org in _discover_orgs():
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
# symlinks.conf block generation
# ---------------------------------------------------------------------------


def _symlink_block(org: str, repo: str) -> str:
    """Generate the symlinks.conf entries for a repo."""
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

_CLAUDE_MD_TEMPLATE = (
    "@AGENTS.md\n\n# {repo}\n\n"
    "> Repo-specific context for Claude Code agents"
    " working in this repository.\n"
)

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


def _record_step(
    steps: list[OnboardingStep],
    *,
    step: int,
    name: str,
    status: str,
    detail: str = "",
    command: str | None = None,
) -> None:
    """Append a stable onboarding step payload."""
    steps.append(
        OnboardingStep(
            step=step,
            name=name,
            status=status,
            detail=detail,
            command=command,
        )
    )


def _failure_result(
    org: str,
    repo: str,
    *,
    error: str,
    steps: list[OnboardingStep],
) -> OnboardingResult:
    """Return a standardized failed onboarding result."""
    return OnboardingResult(org=org, repo=repo, error=error, steps=steps)


def _subprocess_error(stderr: bytes | None, returncode: int) -> str:
    """Normalize a subprocess failure into a short error string."""
    if stderr:
        return stderr.decode().strip()[:200]
    return f"exit code {returncode}"


async def _run_command(*args: str) -> tuple[bool, str]:
    """Execute a subprocess and return success state plus error detail."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        return True, ""
    return False, _subprocess_error(stderr, proc.returncode)


def _ensure_spec_directory(org: str, repo: str) -> str:
    """Create the repo specification directory tree."""
    spec_dir = _SPEC_ROOT / org / repo / "docs" / "specifications"
    spec_dir.mkdir(parents=True, exist_ok=True)
    return str(spec_dir)


def _ensure_orchestration_config(org: str, repo: str) -> str:
    """Create the orchestration mirror directories and default files."""
    orch_dir = _ORCH_ROOT / org / repo
    claude_dir = orch_dir / ".claude"
    cursor_rules_dir = orch_dir / ".cursor" / "rules"
    gemini_dir = orch_dir / ".gemini"

    claude_dir.mkdir(parents=True, exist_ok=True)
    cursor_rules_dir.mkdir(parents=True, exist_ok=True)
    gemini_dir.mkdir(parents=True, exist_ok=True)

    claude_md = orch_dir / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(_CLAUDE_MD_TEMPLATE.format(repo=repo))

    agents_md = orch_dir / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(_AGENTS_MD_TEMPLATE)

    settings_json = claude_dir / "settings.local.json"
    if not settings_json.exists():
        settings_json.write_text(_SETTINGS_LOCAL_JSON)

    return str(orch_dir)


def _update_symlink_registry(org: str, repo: str) -> tuple[str, str]:
    """Append repo entries to the symlink registry when missing."""
    marker = f"core/code/{org}/{repo}/"
    content = _REGISTRY_PATH.read_text() if _REGISTRY_PATH.is_file() else ""
    if marker in content:
        return "skipped", "Already in symlinks.conf"

    block = _symlink_block(org, repo)
    sep = "\n" if content and not content.endswith("\n") else ""
    _REGISTRY_PATH.write_text(content + sep + "\n" + block + "\n")
    return "done", "Added 5 entries"


async def _run_script_step(
    *,
    step: int,
    name: str,
    command: tuple[str, ...],
    success_detail: str,
) -> OnboardingStep:
    """Execute a shell script step and normalize its result."""
    try:
        ok, detail = await _run_command(*command)
    except OSError as exc:
        return OnboardingStep(step=step, name=name, status="failed", detail=str(exc))

    return OnboardingStep(
        step=step,
        name=name,
        status="done" if ok else "failed",
        detail=success_detail if ok else detail,
    )


async def _create_onboarding_issue(
    config: BackboneConfig,
    *,
    title: str,
    body: str,
    labels: list[str],
) -> None:
    """Create and notify an onboarding-related GitHub issue."""
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.routing import create_and_notify

    async with GitHubClient(config) as gh:
        await create_and_notify(
            gh,
            title=title,
            body=body,
            labels=labels,
            config=config,
            flow_name="onboarding",
        )


def _find_org_orchestrator(config: BackboneConfig, org: str) -> str | None:
    """Resolve the concrete orchestrator entity for an organization."""
    return next(
        (
            name
            for name, entry in config.registry.entities.items()
            if entry.organization == org and "orchestrators" in entry.groups
        ),
        None,
    )


async def _notify_brunel(config: BackboneConfig, org: str, repo: str) -> str:
    """Open the post-onboarding verification issue for Brunel."""
    await _create_onboarding_issue(
        config,
        title=f"[task] Verify onboarding infrastructure: {org}/{repo}",
        body=(
            f"## Context\n"
            f"Repo `{org}/{repo}` was just onboarded"
            f" via the automated pipeline.\n\n"
            f"## Request\n"
            f"Verify symlinks, registry entries,"
            f" and SDD setup for `{org}/{repo}`.\n\n"
            f"## References\n"
            f"- Symlinks registry:"
            f" `~/infra/registry/symlinks.conf`\n"
            f"- Repo path:"
            f" `~/ws/core/code/{org}/{repo}/`\n"
        ),
        labels=["from:coding-agent", "for:brunel", "task"],
    )
    return "Created verification issue for brunel"


async def _notify_orchestrator(config: BackboneConfig, org: str, repo: str) -> str:
    """Open the org-orchestrator follow-up issue for new repo setup."""
    orchestrator = _find_org_orchestrator(config, org)
    if not orchestrator:
        raise RuntimeError(f"No orchestrator found for {org}/{repo}")

    await _create_onboarding_issue(
        config,
        title=f"[task] New repo onboarded: {org}/{repo} - needs CLAUDE.md content",
        body=(
            f"## Context\n"
            f"Repo `{org}/{repo}` was just onboarded"
            f" via the automated pipeline.\n\n"
            f"## Request\n"
            f"Create the repo-specific `CLAUDE.md` content for `{org}/{repo}`"
            f" and start the initial orchestration work needed to bring"
            f" the repo into service.\n\n"
            f"## References\n"
            f"- Workspace repo: `~/ws/core/code/{org}/{repo}/`\n"
            f"- Orchestration mirror: `~/orchestration/core/code/{org}/{repo}/`\n"
            f"- Spec docs: `~/ws/core/spec/{org}/{repo}/docs/`\n"
        ),
        labels=["from:coding-agent", f"for:{orchestrator}", "task"],
    )
    return f"Created onboarding issue for {orchestrator}"


async def _notification_step(
    *,
    step: int,
    name: str,
    config: BackboneConfig | None,
    notify: Callable[[BackboneConfig], Awaitable[str]],
) -> OnboardingStep:
    """Run an optional GitHub notification step."""
    if config is None:
        return OnboardingStep(
            step=step,
            name=name,
            status="skipped",
            detail="No config — skipped",
        )

    try:
        detail = await notify(config)
    except Exception as exc:
        return OnboardingStep(
            step=step,
            name=name,
            status="failed",
            detail=str(exc)[:200],
        )

    return OnboardingStep(step=step, name=name, status="done", detail=detail)


async def run_onboarding(
    org: str, url: str, *, config: BackboneConfig | None = None
) -> OnboardingResult:
    """Execute automated onboarding steps for a new repo.

    Accepts an SSH URL (e.g. git@github.com:eandualem/repo.git), clones the repo,
    runs all setup steps, and creates verification issues.
    """
    steps: list[OnboardingStep] = []
    had_failure = False

    # Step 1: Parse SSH URL and extract repo name
    repo = parse_ssh_url(url)
    if not repo:
        _record_step(
            steps,
            step=1,
            name="parse_url",
            status="failed",
            detail=f"Invalid SSH URL: {url}",
        )
        return _failure_result(
            org,
            "",
            error=f"Invalid SSH URL: {url}",
            steps=steps,
        )

    _record_step(steps, step=1, name="parse_url", status="done", detail=repo)

    # Step 2: Clone — HARD GATE (abort on failure)
    target_dir = _WS_ROOT / org / repo
    if target_dir.exists():
        detail_msg = f"Directory already exists: {target_dir}"
        _record_step(
            steps,
            step=2,
            name="clone",
            status="failed",
            detail=detail_msg,
        )
        return _failure_result(
            org,
            repo,
            error=detail_msg,
            steps=steps,
        )

    try:
        ok, detail = await _run_command("git", "clone", url, str(target_dir))
    except OSError as exc:
        detail = str(exc)
        _record_step(
            steps,
            step=2,
            name="clone",
            status="failed",
            detail=detail,
        )
        return _failure_result(
            org,
            repo,
            error=f"Clone failed: {detail}",
            steps=steps,
        )

    if not ok:
        _record_step(steps, step=2, name="clone", status="failed", detail=detail)
        return _failure_result(
            org,
            repo,
            error=f"Clone failed: {detail}",
            steps=steps,
        )

    _record_step(steps, step=2, name="clone", status="done", detail=str(target_dir))

    # Step 3: Create spec directory
    try:
        _record_step(
            steps,
            step=3,
            name="spec_directory",
            status="done",
            detail=_ensure_spec_directory(org, repo),
        )
    except OSError as exc:
        _record_step(
            steps,
            step=3,
            name="spec_directory",
            status="failed",
            detail=str(exc),
        )
        had_failure = True

    # Step 4: Create orchestration config
    try:
        _record_step(
            steps,
            step=4,
            name="orchestration_config",
            status="done",
            detail=_ensure_orchestration_config(org, repo),
        )
    except OSError as exc:
        _record_step(
            steps,
            step=4,
            name="orchestration_config",
            status="failed",
            detail=str(exc),
        )
        had_failure = True

    # Step 5: Append to symlinks.conf
    try:
        status, detail = _update_symlink_registry(org, repo)
        _record_step(
            steps,
            step=5,
            name="registry_entries",
            status=status,
            detail=detail,
        )
    except OSError as exc:
        _record_step(
            steps,
            step=5,
            name="registry_entries",
            status="failed",
            detail=str(exc),
        )
        had_failure = True

    # Step 6: Run setup.sh
    setup_step = await _run_script_step(
        step=6,
        name="setup_script",
        command=(str(_SETUP_SCRIPT),),
        success_detail="setup.sh completed",
    )
    steps.append(setup_step)
    if setup_step.status == "failed":
        had_failure = True

    # Step 7: SDD init — automated
    sdd_step = await _run_script_step(
        step=7,
        name="sdd_init",
        command=(str(_SDD_INIT_SCRIPT), "--org", org, repo),
        success_detail="sdd-init.sh completed",
    )
    steps.append(sdd_step)
    if sdd_step.status == "failed":
        had_failure = True

    # Step 8: Register in repos.json
    try:
        register_repo(org, repo)
        _record_step(
            steps,
            step=8,
            name="repos_json",
            status="done",
            detail=str(_REPOS_JSON),
        )
    except OSError as exc:
        _record_step(
            steps,
            step=8,
            name="repos_json",
            status="failed",
            detail=str(exc),
        )
        had_failure = True

    # Step 9: Notify Brunel (sequential chain: Pipeline → Brunel → Leo → Feynman)
    brunel_step = await _notification_step(
        step=9,
        name="notify_brunel",
        config=config,
        notify=lambda resolved_config: _notify_brunel(resolved_config, org, repo),
    )
    steps.append(brunel_step)
    if brunel_step.status == "failed":
        had_failure = True

    # Step 10: Notify the org orchestrator that repo CLAUDE.md content is needed
    orchestrator_step = await _notification_step(
        step=10,
        name="notify_orchestrator",
        config=config,
        notify=lambda resolved_config: _notify_orchestrator(resolved_config, org, repo),
    )
    steps.append(orchestrator_step)
    if orchestrator_step.status == "failed":
        had_failure = True

    return OnboardingResult(
        org=org,
        repo=repo,
        success=not had_failure,
        steps=steps,
    )
