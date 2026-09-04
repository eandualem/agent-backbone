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
    def test_editable_uses_the_checkouts_commit(self):
        install = release.Installation("editable", "/ws/x")
        ok = MagicMock(returncode=0, stdout="abc123\n", stderr="")
        with patch(f"{_R}.subprocess.run", return_value=ok) as run:
            assert release.code_identity(install) == "git:abc123"
        assert run.call_args.args[0][:4] == ["git", "-C", "/ws/x", "rev-parse"]

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
