"""Tests for agent_backbone.release — installer detection and code identity."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_backbone import release

_R = "agent_backbone.release"


class TestInstallation:
    def test_editable_checkout_from_direct_url(self):
        direct = {"url": "file:///home/u/ws/agent-backbone", "dir_info": {"editable": True}}
        with patch(f"{_R}._direct_url", return_value=direct):
            install = release.installation("/home/u/.local/share/uv/tools/ab/bin/python")
        assert install.kind == "editable"
        assert install.path == "/home/u/ws/agent-backbone"
        assert install.upgrade_command is None
        assert "development checkout" in install.describe()

    def test_uv_tool_from_interpreter_path(self):
        with patch(f"{_R}._direct_url", return_value=None):
            install = release.installation("/home/u/.local/share/uv/tools/ab/bin/python")
        assert install.kind == "uv"
        assert install.upgrade_command == ["uv", "tool", "upgrade", "agent-backbone"]

    def test_pipx_and_other(self):
        with patch(f"{_R}._direct_url", return_value=None):
            pipx = release.installation("/home/u/.local/pipx/venvs/agent-backbone/bin/python")
            other = release.installation("/usr/bin/python3")
        assert pipx.kind == "pipx" and pipx.upgrade_command == ["pipx", "upgrade", "agent-backbone"]
        assert other.kind == "other" and other.upgrade_command is None

    def test_a_non_editable_direct_url_is_not_editable(self):
        with patch(f"{_R}._direct_url", return_value={"url": "https://pypi.org/x.whl"}):
            assert release.installation("/usr/bin/python3").kind == "other"


class TestCodeIdentity:
    def test_editable_reads_branch_and_commit_from_one_git_status(self):
        """One invocation, so the pair cannot straddle a branch switch."""
        install = release.Installation("editable", "/ws/x")
        status = "# branch.oid abc123\n# branch.head develop\n# branch.upstream origin/develop\n"
        ok = MagicMock(returncode=0, stdout=status, stderr="")
        with patch(f"{_R}.subprocess.run", return_value=ok) as run:
            assert release.code_identity(install) == "git:develop@abc123"
        assert run.call_count == 1
        expected = ["git", "-C", "/ws/x", "status", "--porcelain=v2", "--branch"]
        assert run.call_args.args[0] == expected

    def test_a_fresh_repository_without_commits_falls_back_to_the_version(self):
        install = release.Installation("editable", "/ws/x")
        status = "# branch.oid (initial)\n# branch.head main\n"
        ok = MagicMock(returncode=0, stdout=status, stderr="")
        with (
            patch(f"{_R}.subprocess.run", return_value=ok),
            patch(f"{_R}.installed_version", return_value="0.1.0"),
        ):
            assert release.code_identity(install) == "version:0.1.0"

    def test_same_line_is_the_same_branch_or_a_package(self):
        assert release.same_line("git:develop@a", "git:develop@b")
        assert not release.same_line("git:develop@a", "git:feat/x@b")
        assert release.same_line("version:0.1.0", "version:0.1.1")
        assert release.same_line("git:develop@a", "version:0.1.1")  # a reinstall counts

    def test_a_branch_name_may_contain_an_at_sign(self):
        assert release.same_line("git:feature@a@111", "git:feature@a@222")
        assert not release.same_line("git:feature@a@111", "git:feature@b@111")

    def test_editable_without_git_falls_back_to_the_version(self):
        install = release.Installation("editable", "/ws/x")
        bad = MagicMock(returncode=128, stdout="", stderr="not a git repo")
        with (
            patch(f"{_R}.subprocess.run", return_value=bad),
            patch(f"{_R}.installed_version", return_value="1.2.3"),
        ):
            assert release.code_identity(install) == "version:1.2.3"

    def test_package_install_uses_the_installed_version(self):
        with patch(f"{_R}.installed_version", return_value="0.2.0"):
            assert release.code_identity(release.Installation("uv")) == "version:0.2.0"

    def test_latest_published_is_none_when_pypi_is_unreachable(self):
        import httpx

        with patch("httpx.get", side_effect=httpx.ConnectError("offline")):
            assert release.latest_published() is None
