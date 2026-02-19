"""Tests for src/onboarding.py — repo discovery, status checks, and onboarding."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.onboarding import (
    KNOWN_ORGS,
    RepoEntry,
    _check_assume_unchanged,
    _check_git_excludes,
    _check_orch_config,
    _check_registry_entries,
    _check_sparse_checkout,
    _check_spec_dir,
    _check_symlinks_created,
    _symlink_block,
    discover_repos,
    load_repos_json,
    register_repo,
    run_onboarding,
    run_status_checks,
    save_repos_json,
    validate_org,
    validate_repo_name,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    """Set up a fake workspace tree and patch all module-level paths."""
    ws = tmp_path / "ws" / "core" / "code"
    spec = tmp_path / "ws" / "core" / "spec"
    orch = tmp_path / "orchestration" / "core" / "code"
    registry = tmp_path / "infra" / "registry" / "symlinks.conf"
    setup_sh = tmp_path / "infra" / "scripts" / "setup.sh"
    repos_json = tmp_path / "state" / "repos.json"

    # Create base dirs
    ws.mkdir(parents=True)
    spec.mkdir(parents=True)
    orch.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    setup_sh.parent.mkdir(parents=True)
    repos_json.parent.mkdir(parents=True)

    # Create a working setup.sh
    setup_sh.write_text("#!/bin/bash\nexit 0\n")
    setup_sh.chmod(0o755)

    with (
        patch("src.onboarding._WS_ROOT", ws),
        patch("src.onboarding._SPEC_ROOT", spec),
        patch("src.onboarding._ORCH_ROOT", orch),
        patch("src.onboarding._REGISTRY_PATH", registry),
        patch("src.onboarding._SETUP_SCRIPT", setup_sh),
        patch("src.onboarding._REPOS_JSON", repos_json),
    ):
        yield {
            "ws": ws,
            "spec": spec,
            "orch": orch,
            "registry": registry,
            "setup_sh": setup_sh,
            "repos_json": repos_json,
            "root": tmp_path,
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_known_orgs_accepted(self):
        for org in KNOWN_ORGS:
            assert validate_org(org) is True

    def test_unknown_org_rejected(self):
        assert validate_org("FakeOrg") is False
        assert validate_org("") is False

    def test_valid_repo_names(self):
        assert validate_repo_name("my-repo") is True
        assert validate_repo_name("my_repo") is True
        assert validate_repo_name("MyRepo123") is True

    def test_invalid_repo_names(self):
        assert validate_repo_name("") is False
        assert validate_repo_name("../etc") is False
        assert validate_repo_name("repo name") is False
        assert validate_repo_name("repo/nested") is False


# ---------------------------------------------------------------------------
# repos.json
# ---------------------------------------------------------------------------


class TestReposJson:
    def test_round_trip(self, workspace):
        entries = [RepoEntry(org="WF", repo="thing"), RepoEntry(org="Arclio", repo="api")]
        save_repos_json(entries)
        loaded = load_repos_json()
        assert len(loaded) == 2
        assert loaded[0].org == "WF"
        assert loaded[1].repo == "api"

    def test_empty_when_missing(self, workspace):
        assert load_repos_json() == []

    def test_register_dedup(self, workspace):
        register_repo("WF", "new-thing")
        register_repo("WF", "new-thing")  # duplicate
        entries = load_repos_json()
        assert len(entries) == 1

    def test_register_adds_new(self, workspace):
        register_repo("WF", "a")
        register_repo("Arclio", "b")
        entries = load_repos_json()
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discovers_from_filesystem(self, workspace):
        (workspace["ws"] / "WF" / "my-repo").mkdir(parents=True)
        (workspace["ws"] / "Arclio" / "api").mkdir(parents=True)

        repos = discover_repos()
        names = [(r.org, r.repo) for r in repos]
        assert ("WF", "my-repo") in names
        assert ("Arclio", "api") in names

    def test_merges_repos_json(self, workspace):
        """repos.json entries not on disk are included."""
        save_repos_json([RepoEntry(org="WF", repo="not-on-disk")])
        repos = discover_repos()
        names = [(r.org, r.repo) for r in repos]
        assert ("WF", "not-on-disk") in names

    def test_no_duplicates(self, workspace):
        """A repo on disk AND in repos.json appears only once."""
        (workspace["ws"] / "WF" / "my-repo").mkdir(parents=True)
        save_repos_json([RepoEntry(org="WF", repo="my-repo")])

        repos = discover_repos()
        wf_repos = [r for r in repos if r.org == "WF" and r.repo == "my-repo"]
        assert len(wf_repos) == 1

    def test_ignores_hidden_dirs(self, workspace):
        (workspace["ws"] / "WF" / ".hidden").mkdir(parents=True)
        repos = discover_repos()
        names = [r.repo for r in repos]
        assert ".hidden" not in names

    def test_ignores_unknown_orgs(self, workspace):
        (workspace["ws"] / "RandomOrg" / "repo").mkdir(parents=True)
        repos = discover_repos()
        orgs = [r.org for r in repos]
        assert "RandomOrg" not in orgs


# ---------------------------------------------------------------------------
# Status checks
# ---------------------------------------------------------------------------


class TestStatusChecks:
    def test_spec_dir_present(self, workspace):
        (workspace["spec"] / "WF" / "my-repo" / "docs").mkdir(parents=True)
        result = _check_spec_dir("WF", "my-repo")
        assert result.status == "ok"

    def test_spec_dir_missing(self, workspace):
        result = _check_spec_dir("WF", "my-repo")
        assert result.status == "missing"

    def test_orch_config_present(self, workspace):
        d = workspace["orch"] / "WF" / "my-repo"
        d.mkdir(parents=True)
        (d / "CLAUDE.md").write_text("# test")
        result = _check_orch_config("WF", "my-repo")
        assert result.status == "ok"

    def test_orch_config_missing(self, workspace):
        result = _check_orch_config("WF", "my-repo")
        assert result.status == "missing"

    def test_registry_present(self, workspace):
        workspace["registry"].write_text(
            "agent-repo | core/code/WF/my-repo/CLAUDE.md | target | desc\n"
        )
        result = _check_registry_entries("WF", "my-repo")
        assert result.status == "ok"

    def test_registry_missing(self, workspace):
        workspace["registry"].write_text("# empty\n")
        result = _check_registry_entries("WF", "my-repo")
        assert result.status == "missing"

    def test_registry_no_file(self, workspace):
        result = _check_registry_entries("WF", "my-repo")
        assert result.status == "missing"

    def test_symlinks_created(self, workspace):
        repo_dir = workspace["ws"] / "WF" / "my-repo"
        repo_dir.mkdir(parents=True)
        target = workspace["root"] / "target-claude.md"
        target.write_text("# target")
        (repo_dir / "CLAUDE.md").symlink_to(target)
        result = _check_symlinks_created("WF", "my-repo")
        assert result.status == "ok"

    def test_symlinks_not_created(self, workspace):
        repo_dir = workspace["ws"] / "WF" / "my-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "CLAUDE.md").write_text("# regular file")
        result = _check_symlinks_created("WF", "my-repo")
        assert result.status == "missing"

    def test_sparse_checkout_present(self, workspace):
        git_info = workspace["ws"] / "WF" / "my-repo" / ".git" / "info"
        git_info.mkdir(parents=True)
        (git_info / "sparse-checkout").write_text("/*\n!/docs/\n!/CLAUDE.md\n")
        result = _check_sparse_checkout("WF", "my-repo")
        assert result.status == "ok"

    def test_sparse_checkout_missing(self, workspace):
        result = _check_sparse_checkout("WF", "my-repo")
        assert result.status == "missing"

    def test_git_excludes_symlink(self, workspace):
        git_info = workspace["ws"] / "WF" / "my-repo" / ".git" / "info"
        git_info.mkdir(parents=True)
        target = workspace["root"] / "exclude-target"
        target.write_text("# exclude")
        (git_info / "exclude").symlink_to(target)
        result = _check_git_excludes("WF", "my-repo")
        assert result.status == "ok"

    def test_git_excludes_not_symlink(self, workspace):
        git_info = workspace["ws"] / "WF" / "my-repo" / ".git" / "info"
        git_info.mkdir(parents=True)
        (git_info / "exclude").write_text("# regular file")
        result = _check_git_excludes("WF", "my-repo")
        assert result.status == "missing"

    def test_assume_unchanged_with_pattern(self, workspace):
        git_info = workspace["ws"] / "WF" / "my-repo" / ".git" / "info"
        git_info.mkdir(parents=True)
        (git_info / "sparse-checkout").write_text("/*\n!/docs/\n!/CLAUDE.md\n")
        result = _check_assume_unchanged("WF", "my-repo")
        assert result.status == "ok"

    def test_assume_unchanged_without_pattern(self, workspace):
        git_info = workspace["ws"] / "WF" / "my-repo" / ".git" / "info"
        git_info.mkdir(parents=True)
        (git_info / "sparse-checkout").write_text("/*\n")
        result = _check_assume_unchanged("WF", "my-repo")
        assert result.status == "missing"

    def test_assume_unchanged_no_file(self, workspace):
        result = _check_assume_unchanged("WF", "my-repo")
        assert result.status == "missing"

    def test_run_status_checks_all_ok(self, workspace):
        """Fully onboarded repo returns onboarded=True."""
        repo_dir = workspace["ws"] / "WF" / "my-repo"
        repo_dir.mkdir(parents=True)

        # Check 1: spec dir
        (workspace["spec"] / "WF" / "my-repo" / "docs").mkdir(parents=True)
        # Check 2: orch config
        d = workspace["orch"] / "WF" / "my-repo"
        d.mkdir(parents=True)
        (d / "CLAUDE.md").write_text("# test")
        # Check 3: registry
        workspace["registry"].write_text(
            "agent-repo | core/code/WF/my-repo/CLAUDE.md | target | desc\n"
        )
        # Check 4: symlink
        target = workspace["root"] / "claude-target.md"
        target.write_text("# target")
        (repo_dir / "CLAUDE.md").symlink_to(target)
        # Checks 5-7: SDD
        git_info = repo_dir / ".git" / "info"
        git_info.mkdir(parents=True)
        (git_info / "sparse-checkout").write_text("/*\n!/docs/\n!/CLAUDE.md\n")
        exc_target = workspace["root"] / "exclude-target"
        exc_target.write_text("# exclude")
        (git_info / "exclude").symlink_to(exc_target)

        status = run_status_checks("WF", "my-repo")
        assert status.onboarded is True
        assert len(status.checks) == 7

    def test_run_status_checks_partial(self, workspace):
        """Partially onboarded repo returns onboarded=False."""
        (workspace["spec"] / "WF" / "my-repo" / "docs").mkdir(parents=True)
        status = run_status_checks("WF", "my-repo")
        assert status.onboarded is False


# ---------------------------------------------------------------------------
# Block generation
# ---------------------------------------------------------------------------


class TestSymlinkBlock:
    def test_contains_org_and_repo(self):
        block = _symlink_block("WF", "new-thing")
        assert "WF" in block
        assert "new-thing" in block

    def test_has_all_five_entries(self):
        block = _symlink_block("WF", "new-thing")
        lines = [l for l in block.splitlines() if l.strip()]
        assert len(lines) == 5

    def test_entry_types(self):
        block = _symlink_block("Arclio", "my-api")
        assert "agent-repo" in block
        assert "spec-docs" in block
        assert "CLAUDE.md" in block
        assert ".claude" in block
        assert ".cursor" in block
        assert ".gemini" in block


# ---------------------------------------------------------------------------
# Onboarding execution
# ---------------------------------------------------------------------------


class TestRunOnboarding:
    async def test_creates_spec_dir(self, workspace):
        result = await run_onboarding("WF", "new-thing")
        spec_dir = workspace["spec"] / "WF" / "new-thing" / "docs" / "specifications"
        assert spec_dir.is_dir()
        step1 = result.steps[0]
        assert step1.step == 1
        assert step1.status == "done"

    async def test_creates_orch_config(self, workspace):
        result = await run_onboarding("WF", "new-thing")
        orch_dir = workspace["orch"] / "WF" / "new-thing"
        assert (orch_dir / "CLAUDE.md").is_file()
        assert (orch_dir / "AGENTS.md").is_file()
        assert (orch_dir / ".claude" / "settings.local.json").is_file()
        step2 = result.steps[1]
        assert step2.step == 2
        assert step2.status == "done"

    async def test_orch_config_idempotent(self, workspace):
        """Doesn't overwrite existing orch files."""
        orch_dir = workspace["orch"] / "WF" / "new-thing"
        orch_dir.mkdir(parents=True)
        (orch_dir / "CLAUDE.md").write_text("# custom content")

        await run_onboarding("WF", "new-thing")
        assert (orch_dir / "CLAUDE.md").read_text() == "# custom content"

    async def test_appends_to_registry(self, workspace):
        workspace["registry"].write_text("# existing\n")
        await run_onboarding("WF", "new-thing")
        content = workspace["registry"].read_text()
        assert "core/code/WF/new-thing/CLAUDE.md" in content
        assert "# existing" in content

    async def test_registry_skip_if_present(self, workspace):
        workspace["registry"].write_text(
            "agent-repo | core/code/WF/new-thing/CLAUDE.md | target | desc\n"
        )
        result = await run_onboarding("WF", "new-thing")
        step3 = result.steps[2]
        assert step3.status == "skipped"

    async def test_setup_sh_success(self, workspace):
        result = await run_onboarding("WF", "new-thing")
        step4 = result.steps[3]
        assert step4.step == 4
        assert step4.status == "done"

    async def test_setup_sh_failure(self, workspace):
        workspace["setup_sh"].write_text("#!/bin/bash\nexit 1\n")
        workspace["setup_sh"].chmod(0o755)
        result = await run_onboarding("WF", "new-thing")
        step4 = result.steps[3]
        assert step4.status == "failed"
        assert result.success is False

    async def test_sdd_manual_step(self, workspace):
        result = await run_onboarding("WF", "new-thing")
        step5 = result.steps[4]
        assert step5.step == 5
        assert step5.status == "manual_required"
        assert "sdd-init.sh" in step5.command

    async def test_registers_in_repos_json(self, workspace):
        await run_onboarding("WF", "new-thing")
        entries = load_repos_json()
        assert any(e.org == "WF" and e.repo == "new-thing" for e in entries)

    async def test_success_when_all_ok(self, workspace):
        result = await run_onboarding("WF", "new-thing")
        assert result.success is True
        assert len(result.steps) == 6
