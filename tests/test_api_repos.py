"""Tests for api/routes/repos.py — repository onboarding endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.onboarding import RepoEntry, save_repos_json


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
    repos_json = tmp_path / "state" / "repos.json"

    ws.mkdir(parents=True)
    spec.mkdir(parents=True)
    orch.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    setup_sh.parent.mkdir(parents=True)
    repos_json.parent.mkdir(parents=True)

    # Working setup.sh
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
        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "WF", "repo": "new-thing"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["org"] == "WF"
        assert data["repo"] == "new-thing"
        assert data["success"] is True
        assert len(data["steps"]) == 6

        # Verify spec dir created
        assert (repo_workspace["spec"] / "WF" / "new-thing" / "docs" / "specifications").is_dir()
        # Verify orch config created
        assert (repo_workspace["orch"] / "WF" / "new-thing" / "CLAUDE.md").is_file()
        assert (repo_workspace["orch"] / "WF" / "new-thing" / ".claude" / "settings.local.json").is_file()
        # Verify registry entries
        assert "core/code/WF/new-thing/" in repo_workspace["registry"].read_text()

    async def test_invalid_org_returns_400(self, client, auth_headers, repo_workspace):
        """Unknown org returns 400."""
        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "FakeOrg", "repo": "thing"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_invalid_repo_name_returns_400(self, client, auth_headers, repo_workspace):
        """Invalid repo name returns 400."""
        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "WF", "repo": "../escape"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_idempotent_registry(self, client, auth_headers, repo_workspace):
        """Re-running onboard doesn't duplicate registry entries."""
        repo_workspace["registry"].write_text(
            "agent-repo | core/code/WF/new-thing/CLAUDE.md | target | desc\n"
        )

        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "WF", "repo": "new-thing"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        data = resp.json()
        registry_step = next(s for s in data["steps"] if s["name"] == "registry_entries")
        assert registry_step["status"] == "skipped"

    async def test_manual_step_returned(self, client, auth_headers, repo_workspace):
        """Step 5 is returned as manual_required with sdd-init command."""
        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "WF", "repo": "new-thing"},
            headers=auth_headers,
        )

        data = resp.json()
        sdd_step = next(s for s in data["steps"] if s["name"] == "sdd_init")
        assert sdd_step["status"] == "manual_required"
        assert "sdd-init.sh" in sdd_step["command"]

    async def test_setup_sh_failure(self, client, auth_headers, repo_workspace):
        """Reports failure when setup.sh exits non-zero."""
        repo_workspace["setup_sh"].write_text("#!/bin/bash\nexit 1\n")
        repo_workspace["setup_sh"].chmod(0o755)

        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "WF", "repo": "new-thing"},
            headers=auth_headers,
        )

        data = resp.json()
        assert data["success"] is False
        setup_step = next(s for s in data["steps"] if s["name"] == "setup_script")
        assert setup_step["status"] == "failed"

    async def test_requires_auth(self, client, api_key, repo_workspace):
        """Returns 401 without auth header."""
        resp = await client.post(
            "/api/repos/onboard",
            json={"org": "WF", "repo": "thing"},
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
