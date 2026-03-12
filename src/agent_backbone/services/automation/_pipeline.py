"""Onboarding pipeline — discovery, validation, registry, and automated setup.

Contains the 10-step onboarding pipeline for new repos, filesystem discovery,
repo registry (repos.json) management, and validation utilities.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
        steps.append(
            OnboardingStep(
                step=1,
                name="parse_url",
                status="failed",
                detail=f"Invalid SSH URL: {url}",
            )
        )
        return OnboardingResult(
            org=org,
            repo="",
            error=f"Invalid SSH URL: {url}",
            steps=steps,
        )

    steps.append(OnboardingStep(step=1, name="parse_url", status="done", detail=repo))

    # Step 2: Clone — HARD GATE (abort on failure)
    target_dir = _WS_ROOT / org / repo
    if target_dir.exists():
        detail_msg = f"Directory already exists: {target_dir}"
        steps.append(
            OnboardingStep(
                step=2,
                name="clone",
                status="failed",
                detail=detail_msg,
            )
        )
        return OnboardingResult(
            org=org,
            repo=repo,
            error=detail_msg,
            steps=steps,
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            url,
            str(target_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            steps.append(
                OnboardingStep(
                    step=2,
                    name="clone",
                    status="done",
                    detail=str(target_dir),
                )
            )
        else:
            err_msg = stderr.decode().strip()[:200] if stderr else f"exit code {proc.returncode}"
            steps.append(
                OnboardingStep(
                    step=2,
                    name="clone",
                    status="failed",
                    detail=err_msg,
                )
            )
            return OnboardingResult(
                org=org,
                repo=repo,
                error=f"Clone failed: {err_msg}",
                steps=steps,
            )
    except OSError as exc:
        steps.append(
            OnboardingStep(
                step=2,
                name="clone",
                status="failed",
                detail=str(exc),
            )
        )
        return OnboardingResult(
            org=org,
            repo=repo,
            error=f"Clone failed: {exc}",
            steps=steps,
        )

    # Step 3: Create spec directory
    spec_dir = _SPEC_ROOT / org / repo / "docs" / "specifications"
    try:
        spec_dir.mkdir(parents=True, exist_ok=True)
        steps.append(
            OnboardingStep(
                step=3,
                name="spec_directory",
                status="done",
                detail=str(spec_dir),
            )
        )
    except OSError as exc:
        steps.append(
            OnboardingStep(
                step=3,
                name="spec_directory",
                status="failed",
                detail=str(exc),
            )
        )
        had_failure = True

    # Step 4: Create orchestration config
    orch_dir = _ORCH_ROOT / org / repo
    claude_dir = orch_dir / ".claude"
    cursor_rules_dir = orch_dir / ".cursor" / "rules"
    gemini_dir = orch_dir / ".gemini"
    try:
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

        steps.append(
            OnboardingStep(
                step=4,
                name="orchestration_config",
                status="done",
                detail=str(orch_dir),
            )
        )
    except OSError as exc:
        steps.append(
            OnboardingStep(
                step=4,
                name="orchestration_config",
                status="failed",
                detail=str(exc),
            )
        )
        had_failure = True

    # Step 5: Append to symlinks.conf
    try:
        marker = f"core/code/{org}/{repo}/"
        content = _REGISTRY_PATH.read_text() if _REGISTRY_PATH.is_file() else ""
        if marker in content:
            steps.append(
                OnboardingStep(
                    step=5,
                    name="registry_entries",
                    status="skipped",
                    detail="Already in symlinks.conf",
                )
            )
        else:
            block = _symlink_block(org, repo)
            # Ensure trailing newline before appending
            sep = "\n" if content and not content.endswith("\n") else ""
            _REGISTRY_PATH.write_text(content + sep + "\n" + block + "\n")
            steps.append(
                OnboardingStep(
                    step=5,
                    name="registry_entries",
                    status="done",
                    detail=f"Added {5} entries",
                )
            )
    except OSError as exc:
        steps.append(
            OnboardingStep(
                step=5,
                name="registry_entries",
                status="failed",
                detail=str(exc),
            )
        )
        had_failure = True

    # Step 6: Run setup.sh
    try:
        proc = await asyncio.create_subprocess_exec(
            str(_SETUP_SCRIPT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            steps.append(
                OnboardingStep(
                    step=6,
                    name="setup_script",
                    status="done",
                    detail="setup.sh completed",
                )
            )
        else:
            err_msg = stderr.decode().strip()[:200] if stderr else f"exit code {proc.returncode}"
            steps.append(
                OnboardingStep(
                    step=6,
                    name="setup_script",
                    status="failed",
                    detail=err_msg,
                )
            )
            had_failure = True
    except OSError as exc:
        steps.append(
            OnboardingStep(
                step=6,
                name="setup_script",
                status="failed",
                detail=str(exc),
            )
        )
        had_failure = True

    # Step 7: SDD init — automated
    try:
        proc = await asyncio.create_subprocess_exec(
            str(_SDD_INIT_SCRIPT),
            "--org",
            org,
            repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            steps.append(
                OnboardingStep(
                    step=7,
                    name="sdd_init",
                    status="done",
                    detail="sdd-init.sh completed",
                )
            )
        else:
            err_msg = stderr.decode().strip()[:200] if stderr else f"exit code {proc.returncode}"
            steps.append(
                OnboardingStep(
                    step=7,
                    name="sdd_init",
                    status="failed",
                    detail=err_msg,
                )
            )
            had_failure = True
    except OSError as exc:
        steps.append(
            OnboardingStep(
                step=7,
                name="sdd_init",
                status="failed",
                detail=str(exc),
            )
        )
        had_failure = True

    # Step 8: Register in repos.json
    try:
        register_repo(org, repo)
        steps.append(
            OnboardingStep(
                step=8,
                name="repos_json",
                status="done",
                detail=str(_REPOS_JSON),
            )
        )
    except OSError as exc:
        steps.append(
            OnboardingStep(
                step=8,
                name="repos_json",
                status="failed",
                detail=str(exc),
            )
        )
        had_failure = True

    # Step 9: Notify Brunel (sequential chain: Pipeline → Brunel → Leo → Feynman)
    if config is not None:
        try:
            from agent_backbone.services.github import GitHubClient
            from agent_backbone.services.routing import create_and_notify

            async with GitHubClient(config) as gh:
                await create_and_notify(
                    gh,
                    title=(f"[task] Verify onboarding infrastructure: {org}/{repo}"),
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
                    config=config,
                    flow_name="onboarding",
                )
            steps.append(
                OnboardingStep(
                    step=9,
                    name="notify_brunel",
                    status="done",
                    detail="Created verification issue for brunel",
                )
            )
        except Exception as exc:
            steps.append(
                OnboardingStep(
                    step=9,
                    name="notify_brunel",
                    status="failed",
                    detail=str(exc)[:200],
                )
            )
            had_failure = True
    else:
        steps.append(
            OnboardingStep(
                step=9,
                name="notify_brunel",
                status="skipped",
                detail="No config — skipped",
            )
        )

    # Step 10: Notify the org orchestrator that repo CLAUDE.md content is needed
    if config is not None:
        try:
            from agent_backbone.services.github import GitHubClient
            from agent_backbone.services.routing import create_and_notify

            orchestrator = next(
                (
                    name
                    for name, entry in config.registry.entities.items()
                    if entry.organization == org and "orchestrators" in entry.groups
                ),
                None,
            )
            if not orchestrator:
                raise RuntimeError(f"No orchestrator found for {org}/{repo}")

            async with GitHubClient(config) as gh:
                await create_and_notify(
                    gh,
                    title=(f"[task] New repo onboarded: {org}/{repo} - needs CLAUDE.md content"),
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
                    config=config,
                    flow_name="onboarding",
                )
            steps.append(
                OnboardingStep(
                    step=10,
                    name="notify_orchestrator",
                    status="done",
                    detail=f"Created onboarding issue for {orchestrator}",
                )
            )
        except Exception as exc:
            steps.append(
                OnboardingStep(
                    step=10,
                    name="notify_orchestrator",
                    status="failed",
                    detail=str(exc)[:200],
                )
            )
            had_failure = True
    else:
        steps.append(
            OnboardingStep(
                step=10,
                name="notify_orchestrator",
                status="skipped",
                detail="No config — skipped",
            )
        )

    return OnboardingResult(
        org=org,
        repo=repo,
        success=not had_failure,
        steps=steps,
    )
