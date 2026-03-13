"""Tests for api/routes/repos.py — repository onboarding endpoints."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_backbone.services.registry import EntityEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SSH_URL = "git@github.com:eandualem/new-thing.git"


def _mock_subprocess_ok():
    """Create a mock process that exits 0."""
    proc = AsyncMock()
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 0
    return proc


def _selective_create_subprocess_exec(real_func, clone_proc):
    """Return a side_effect that mocks git clone but delegates other calls."""

    async def _side_effect(*args, **kwargs):
        if args and args[0] == "git":
            return clone_proc
        return await real_func(*args, **kwargs)

    return _side_effect


def _enable_wf_onboarding_success(client):
    """Add a concrete WF orchestrator so onboarding can complete route-side."""
    client._transport.app.state.config.registry.entities["bell-wf"] = EntityEntry(
        session="bell-wf",
        home="~/ws/core/code/WF/bell",
        groups=["orchestrators"],
        figure="Bell",
        role="WF Orchestrator",
        organization="WF",
        entity_type="role-instance",
    )


@pytest.fixture
async def client(api_app):
    """Async test client bound to the api app."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def repo_workspace(tmp_path):
    """Patch all onboarding module-level paths to use tmp_path."""
    ws = tmp_path / "ws" / "core" / "code"
    spec = tmp_path / "ws" / "core" / "spec"
    orch = tmp_path / "orchestration" / "core" / "code"
    registry = tmp_path / "infra" / "registry" / "symlinks.conf"
    setup_sh = tmp_path / "infra" / "scripts" / "setup.sh"
    sdd_init_sh = tmp_path / "infra" / "scripts" / "sdd-init.sh"
    repos_json = tmp_path / "state" / "repos.json"

    ws.mkdir(parents=True)
    # Create known org directories so _discover_orgs() finds them
    (ws / "Arclio").mkdir()
    (ws / "Loveble").mkdir()
    (ws / "WF").mkdir()
    spec.mkdir(parents=True)
    orch.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    setup_sh.parent.mkdir(parents=True)
    repos_json.parent.mkdir(parents=True)

    # Working scripts
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
        }


# ---------------------------------------------------------------------------
# GET /api/repos
# ---------------------------------------------------------------------------


class TestListRepos:
    async def test_returns_repos_with_checks(self, client, auth_headers, repo_workspace):
        """Returns discovered repos with 7 status checks each."""
        (repo_workspace["ws"] / "WF" / "my-repo").mkdir(parents=True)

        resp = await client.get("/api/repos", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        repo_item = next(r for r in data["items"] if r["repo"] == "my-repo")
        assert repo_item["org"] == "WF"
        assert len(repo_item["checks"]) == 7

    async def test_empty_when_no_repos(self, client, auth_headers, repo_workspace):
        """Returns empty list when no repos discovered."""
        resp = await client.get("/api/repos", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    async def test_requires_auth(self, client, api_key, repo_workspace):
        """Returns 401 without auth header."""
        resp = await client.get("/api/repos")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/repos/onboard
# ---------------------------------------------------------------------------


class TestOnboardRepo:
    async def test_creates_scaffolding(self, client, auth_headers, repo_workspace):
        """Creates spec dir, orch config, registry entries."""
        _enable_wf_onboarding_success(client)
        clone_proc = _mock_subprocess_ok()
        mock_gh = AsyncMock()
        mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
        mock_gh.__aexit__ = AsyncMock(return_value=False)

        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with (
            patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect),
            patch("agent_backbone.services.github.GitHubClient", return_value=mock_gh),
            patch(
                "agent_backbone.services.routing.create_and_notify",
                new_callable=AsyncMock,
            ),
        ):
            resp = await client.post(
                "/api/repos/onboard",
                json={"org": "WF", "url": _SSH_URL},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["org"] == "WF"
        assert data["repo"] == "new-thing"
        assert data["success"] is True
        assert len(data["steps"]) == 10
        assert data["steps"][-2]["name"] == "notify_brunel"
        assert data["steps"][-2]["status"] == "done"
        assert data["steps"][-1]["name"] == "notify_orchestrator"
        assert data["steps"][-1]["status"] == "done"

        # Verify spec dir created
        spec_path = repo_workspace["spec"] / "WF" / "new-thing" / "docs" / "specifications"
        assert spec_path.is_dir()
        # Verify orch config created
        orch_base = repo_workspace["orch"] / "WF" / "new-thing"
        assert (orch_base / "CLAUDE.md").is_file()
        settings_path = orch_base / ".claude" / "settings.local.json"
        assert settings_path.is_file()
        # Verify registry entries
        assert "core/code/WF/new-thing/" in repo_workspace["registry"].read_text()

    async def test_invalid_org_returns_400(self, client, auth_headers, repo_workspace):
        """Unknown org returns 400."""
        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "FakeOrg", "url": _SSH_URL},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_invalid_url_returns_failure(self, client, auth_headers, repo_workspace):
        """Invalid SSH URL returns 201 with success=False (parse step fails)."""
        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "WF", "url": "https://not-ssh.com/repo"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["success"] is False
        assert "Invalid SSH URL" in data["error"]

    async def test_idempotent_registry(self, client, auth_headers, repo_workspace):
        """Re-running onboard doesn't duplicate registry entries."""
        repo_workspace["registry"].write_text(
            "agent-repo | core/code/WF/new-thing/CLAUDE.md | target | desc\n"
        )

        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            resp = await client.post(
                "/api/repos/onboard",
                json={"org": "WF", "url": _SSH_URL},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        registry_step = next(s for s in data["steps"] if s["name"] == "registry_entries")
        assert registry_step["status"] == "skipped"

    async def test_sdd_init_automated(self, client, auth_headers, repo_workspace):
        """Step 7 (sdd_init) is now automated, not manual_required."""
        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            resp = await client.post(
                "/api/repos/onboard",
                json={"org": "WF", "url": _SSH_URL},
                headers=auth_headers,
            )

        data = resp.json()
        sdd_step = next(s for s in data["steps"] if s["name"] == "sdd_init")
        assert sdd_step["status"] == "done"

    async def test_setup_sh_failure(self, client, auth_headers, repo_workspace):
        """Reports failure when setup.sh exits non-zero."""
        repo_workspace["setup_sh"].write_text("#!/bin/bash\nexit 1\n")
        repo_workspace["setup_sh"].chmod(0o755)

        clone_proc = _mock_subprocess_ok()
        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            resp = await client.post(
                "/api/repos/onboard",
                json={"org": "WF", "url": _SSH_URL},
                headers=auth_headers,
            )

        data = resp.json()
        assert data["success"] is False
        setup_step = next(s for s in data["steps"] if s["name"] == "setup_script")
        assert setup_step["status"] == "failed"

    async def test_clone_failure_aborts(self, client, auth_headers, repo_workspace):
        """Clone failure returns only parse + clone steps."""
        clone_proc = AsyncMock()
        clone_proc.communicate.return_value = (b"", b"fatal: repo not found")
        clone_proc.returncode = 128

        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect):
            resp = await client.post(
                "/api/repos/onboard",
                json={"org": "WF", "url": _SSH_URL},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["success"] is False
        assert len(data["steps"]) == 2

    async def test_registry_refreshed_after_onboard(self, client, auth_headers, repo_workspace):
        """After successful onboard, config.registry includes the new repo."""
        _enable_wf_onboarding_success(client)
        clone_proc = _mock_subprocess_ok()
        mock_gh = AsyncMock()
        mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
        mock_gh.__aexit__ = AsyncMock(return_value=False)

        _clone_side_effect = _selective_create_subprocess_exec(
            asyncio.create_subprocess_exec, clone_proc
        )
        with (
            patch("asyncio.create_subprocess_exec", side_effect=_clone_side_effect),
            patch("agent_backbone.services.github.GitHubClient", return_value=mock_gh),
            patch(
                "agent_backbone.services.routing.create_and_notify",
                new_callable=AsyncMock,
            ),
        ):
            resp = await client.post(
                "/api/repos/onboard",
                json={"org": "WF", "url": _SSH_URL},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["success"] is True

        # Verify the registry was updated
        config = client._transport.app.state.config
        assert "new-thing" in config.registry.repo_names

    async def test_requires_auth(self, client, api_key, repo_workspace):
        """Returns 401 without auth header."""
        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "WF", "url": _SSH_URL},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/repos/{org}/{repo}/status
# ---------------------------------------------------------------------------


class TestGetRepoStatus:
    async def test_returns_7_checks(self, client, auth_headers, repo_workspace):
        """Returns status with 7 checks for a known repo."""
        resp = await client.get("/api/repos/WF/my-repo/status", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["org"] == "WF"
        assert data["repo"] == "my-repo"
        assert len(data["checks"]) == 7

    async def test_fully_onboarded(self, client, auth_headers, repo_workspace):
        """Fully onboarded repo shows onboarded=True."""
        repo_dir = repo_workspace["ws"] / "WF" / "my-repo"
        repo_dir.mkdir(parents=True)

        # All 7 checks
        (repo_workspace["spec"] / "WF" / "my-repo" / "docs").mkdir(parents=True)
        d = repo_workspace["orch"] / "WF" / "my-repo"
        d.mkdir(parents=True)
        (d / "CLAUDE.md").write_text("# test")
        repo_workspace["registry"].write_text(
            "agent-repo | core/code/WF/my-repo/CLAUDE.md | t | d\n"
        )
        target = repo_workspace["ws"].parent / "target.md"
        target.write_text("# target")
        (repo_dir / "CLAUDE.md").symlink_to(target)
        git_info = repo_dir / ".git" / "info"
        git_info.mkdir(parents=True)
        (git_info / "sparse-checkout").write_text("/*\n!/docs/\n!/CLAUDE.md\n")
        exc_target = repo_workspace["ws"].parent / "exclude"
        exc_target.write_text("# exc")
        (git_info / "exclude").symlink_to(exc_target)

        resp = await client.get("/api/repos/WF/my-repo/status", headers=auth_headers)

        data = resp.json()
        assert data["onboarded"] is True

    async def test_invalid_org_returns_400(self, client, auth_headers, repo_workspace):
        """Unknown org returns 400."""
        resp = await client.get("/api/repos/FakeOrg/thing/status", headers=auth_headers)
        assert resp.status_code == 400

    async def test_invalid_repo_returns_400(self, client, auth_headers, repo_workspace):
        """Invalid repo name returns 400."""
        resp = await client.get("/api/repos/WF/bad..name/status", headers=auth_headers)
        assert resp.status_code == 400

    async def test_requires_auth(self, client, api_key, repo_workspace):
        """Returns 401 without auth header."""
        resp = await client.get("/api/repos/WF/thing/status")
        assert resp.status_code == 401
