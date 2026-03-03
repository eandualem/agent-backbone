"""Tests for agent_backbone/onboarding.py — repo discovery, status checks, and onboarding."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.automation import (
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
    parse_ssh_url,
    register_repo,
    run_onboarding,
    run_status_checks,
    save_repos_json,
    validate_org,
    validate_repo_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SSH_URL = "git@github.com:eandualem/new-thing.git"
_SSH_URL_NO_GIT = "git@github.com:eandualem/new-thing"


def _mock_subprocess_ok():
    """Create a mock process that exits 0."""
    proc = AsyncMock()
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 0
    return proc


def _mock_subprocess_fail(code: int = 128, stderr: bytes = b"fatal: error"):
    """Create a mock process that exits non-zero."""
    proc = AsyncMock()
    proc.communicate.return_value = (b"", stderr)
    proc.returncode = code
    return proc


def _selective_create_subprocess_exec(real_func, clone_proc):
    """Return a side_effect that mocks git clone but delegates other calls."""

    async def _side_effect(*args, **kwargs):
        if args and args[0] == "git":
            return clone_proc
        return await real_func(*args, **kwargs)

    return _side_effect


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
    sdd_init_sh = tmp_path / "infra" / "scripts" / "sdd-init.sh"
    repos_json = tmp_path / "state" / "repos.json"

    # Create base dirs
    ws.mkdir(parents=True)
    spec.mkdir(parents=True)
    orch.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    setup_sh.parent.mkdir(parents=True)
    repos_json.parent.mkdir(parents=True)

    # Create working scripts
    setup_sh.write_text("#!/bin/bash\nexit 0\n")
    setup_sh.chmod(0o755)
    sdd_init_sh.write_text("#!/bin/bash\nexit 0\n")
    sdd_init_sh.chmod(0o755)

    with (
        patch("agent_backbone.services.automation._pipeline._WS_ROOT", ws),
        patch("agent_backbone.services.automation._pipeline._SPEC_ROOT", spec),
        patch("agent_backbone.services.automation._pipeline._ORCH_ROOT", orch),
        patch("agent_backbone.services.automation._pipeline._REGISTRY_PATH", registry),
        patch("agent_backbone.services.automation._pipeline._SETUP_SCRIPT", setup_sh),
        patch("agent_backbone.services.automation._pipeline._SDD_INIT_SCRIPT", sdd_init_sh),
        patch("agent_backbone.services.automation._pipeline._REPOS_JSON", repos_json),
    ):
        yield {
            "ws": ws,
            "spec": spec,
            "orch": orch,
            "registry": registry,
            "setup_sh": setup_sh,
            "sdd_init_sh": sdd_init_sh,
            "repos_json": repos_json,
            "root": tmp_path,
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_known_orgs_accepted(self, workspace):
        """Org dirs under the workspace root are accepted by validate_org."""
        (workspace["ws"] / "WF").mkdir(parents=True, exist_ok=True)
        (workspace["ws"] / "Arclio").mkdir(parents=True, exist_ok=True)
        assert validate_org("WF") is True
        assert validate_org("Arclio") is True

    def test_unknown_org_rejected(self, workspace):
        """Orgs not present as dirs under workspace root are rejected."""
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
# SSH URL parsing
# ---------------------------------------------------------------------------


class TestParseSshUrl:
    def test_valid_with_git_suffix(self):
        assert parse_ssh_url("git@github.com:eandualem/my-repo.git") == "my-repo"

    def test_valid_without_git_suffix(self):
        assert parse_ssh_url("git@github.com:eandualem/my-repo") == "my-repo"

    def test_valid_with_dots_in_name(self):
        assert parse_ssh_url("git@github.com:org/repo.name.git") == "repo.name"

    def test_valid_with_underscores(self):
        assert parse_ssh_url("git@gitlab.com:user/my_repo.git") == "my_repo"

    def test_invalid_https_url(self):
        assert parse_ssh_url("https://github.com/eandualem/repo.git") is None

    def test_invalid_empty_string(self):
        assert parse_ssh_url("") is None

    def test_invalid_no_colon(self):
        assert parse_ssh_url("git@github.com/eandualem/repo.git") is None

    def test_invalid_malformed(self):
        assert parse_ssh_url("not-a-url") is None


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

    def test_discovers_all_org_dirs(self, workspace):
        """All directories under workspace root are treated as orgs."""
        (workspace["ws"] / "RandomOrg" / "repo").mkdir(parents=True)
        repos = discover_repos()
        orgs = [r.org for r in repos]
        assert "RandomOrg" in orgs


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
        lines = [line for line in block.splitlines() if line.strip()]
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
    async def test_parse_url_extracts_repo(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        assert result.repo == "new-thing"
        step1 = result.steps[0]
        assert step1.step == 1
        assert step1.name == "parse_url"
        assert step1.status == "done"

    async def test_invalid_url_aborts(self, workspace):
        result = await run_onboarding("WF", "https://github.com/bad/url")
        assert result.success is False
        assert result.error.startswith("Invalid SSH URL")
        assert len(result.steps) == 1
        assert result.steps[0].status == "failed"

    async def test_clone_success(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        step2 = result.steps[1]
        assert step2.step == 2
        assert step2.name == "clone"
        assert step2.status == "done"

    async def test_clone_failure_aborts(self, workspace):
        clone_proc = _mock_subprocess_fail()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        assert result.success is False
        assert "Clone failed" in result.error
        assert len(result.steps) == 2
        assert result.steps[1].status == "failed"

    async def test_clone_dir_exists_aborts(self, workspace):
        (workspace["ws"] / "WF" / "new-thing").mkdir(parents=True)
        result = await run_onboarding("WF", _SSH_URL)
        assert result.success is False
        assert "already exists" in result.error
        assert len(result.steps) == 2
        assert result.steps[1].status == "failed"

    async def test_creates_spec_dir(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        spec_dir = workspace["spec"] / "WF" / "new-thing" / "docs" / "specifications"
        assert spec_dir.is_dir()
        step3 = result.steps[2]
        assert step3.step == 3
        assert step3.name == "spec_directory"
        assert step3.status == "done"

    async def test_creates_orch_config(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        orch_dir = workspace["orch"] / "WF" / "new-thing"
        assert (orch_dir / "CLAUDE.md").is_file()
        assert (orch_dir / "AGENTS.md").is_file()
        assert (orch_dir / ".claude" / "settings.local.json").is_file()
        step4 = result.steps[3]
        assert step4.step == 4
        assert step4.name == "orchestration_config"
        assert step4.status == "done"

    async def test_orch_config_idempotent(self, workspace):
        """Doesn't overwrite existing orch files."""
        orch_dir = workspace["orch"] / "WF" / "new-thing"
        orch_dir.mkdir(parents=True)
        (orch_dir / "CLAUDE.md").write_text("# custom content")

        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            await run_onboarding("WF", _SSH_URL)
        assert (orch_dir / "CLAUDE.md").read_text() == "# custom content"

    async def test_appends_to_registry(self, workspace):
        workspace["registry"].write_text("# existing\n")
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            await run_onboarding("WF", _SSH_URL)
        content = workspace["registry"].read_text()
        assert "core/code/WF/new-thing/CLAUDE.md" in content
        assert "# existing" in content

    async def test_registry_skip_if_present(self, workspace):
        workspace["registry"].write_text(
            "agent-repo | core/code/WF/new-thing/CLAUDE.md | target | desc\n"
        )
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        step5 = result.steps[4]
        assert step5.name == "registry_entries"
        assert step5.status == "skipped"

    async def test_setup_sh_success(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        step6 = result.steps[5]
        assert step6.step == 6
        assert step6.name == "setup_script"
        assert step6.status == "done"

    async def test_setup_sh_failure(self, workspace):
        workspace["setup_sh"].write_text("#!/bin/bash\nexit 1\n")
        workspace["setup_sh"].chmod(0o755)
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        step6 = result.steps[5]
        assert step6.name == "setup_script"
        assert step6.status == "failed"
        assert result.success is False

    async def test_sdd_init_success(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        step7 = result.steps[6]
        assert step7.step == 7
        assert step7.name == "sdd_init"
        assert step7.status == "done"

    async def test_sdd_init_failure(self, workspace):
        workspace["sdd_init_sh"].write_text("#!/bin/bash\nexit 1\n")
        workspace["sdd_init_sh"].chmod(0o755)
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        step7 = result.steps[6]
        assert step7.name == "sdd_init"
        assert step7.status == "failed"
        assert result.success is False

    async def test_registers_in_repos_json(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            await run_onboarding("WF", _SSH_URL)
        entries = load_repos_json()
        assert any(e.org == "WF" and e.repo == "new-thing" for e in entries)

    async def test_notify_brunel_created(self, workspace):
        clone_proc = _mock_subprocess_ok()
        mock_gh = AsyncMock()
        mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
        mock_gh.__aexit__ = AsyncMock(return_value=False)

        from agent_backbone.config import BackboneConfig

        cfg = BackboneConfig(github_token="test-token")

        mock_create_notify = AsyncMock()

        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with (
            patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect),
            patch("agent_backbone.services.github.GitHubClient", return_value=mock_gh),
            patch(
                "agent_backbone.services.routing.create_and_notify",
                mock_create_notify,
            ),
        ):
            result = await run_onboarding("WF", _SSH_URL, config=cfg)

        step9 = result.steps[8]
        assert step9.step == 9
        assert step9.name == "notify_brunel"
        assert step9.status == "done"
        assert step9.detail == "Created verification issue for brunel"
        # Only one call to create_and_notify (Brunel only)
        assert mock_create_notify.call_count == 1
        call_kwargs = mock_create_notify.call_args.kwargs
        assert "for:brunel" in call_kwargs["labels"]
        assert call_kwargs["flow_name"] == "onboarding"
        assert call_kwargs["config"] is cfg

    async def test_notify_brunel_skipped_no_config(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL, config=None)
        step9 = result.steps[8]
        assert step9.name == "notify_brunel"
        assert step9.status == "skipped"

    async def test_notify_brunel_skipped_no_token(self, workspace):
        clone_proc = _mock_subprocess_ok()
        from agent_backbone.config import BackboneConfig

        cfg = BackboneConfig(github_token="")
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL, config=cfg)
        step9 = result.steps[8]
        assert step9.name == "notify_brunel"
        assert step9.status == "skipped"

    async def test_success_when_all_ok(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL)
        assert result.success is True
        assert len(result.steps) == 9

    async def test_url_without_git_suffix(self, workspace):
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            result = await run_onboarding("WF", _SSH_URL_NO_GIT)
        assert result.repo == "new-thing"
        assert result.steps[0].status == "done"
