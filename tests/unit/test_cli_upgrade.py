"""`backbone upgrade` — new code in, one restart, agents untouched."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone import cli
from agent_backbone.cli import upgrade
from agent_backbone.config import bootstrap_config
from agent_backbone.release import Installation

_UP = "agent_backbone.cli.upgrade"


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return int(exc.value.code or 0)


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "data"))


class TestUpgradeCommand:
    def test_check_reports_versions_only(self, capsys):
        with (
            patch(f"{_UP}.installation", return_value=Installation("uv")),
            patch(f"{_UP}.installed_version", return_value="0.1.0"),
            patch(f"{_UP}.latest_published", return_value="0.1.1"),
            patch(f"{_UP}.subprocess.run") as run,
            patch(f"{_UP}.restart_backbone", new_callable=AsyncMock) as restart,
        ):
            assert _run(["upgrade", "--check"]) == 0
        out = capsys.readouterr().out
        assert "installed: 0.1.0" in out and "latest on PyPI: 0.1.1" in out
        run.assert_not_called()
        restart.assert_not_called()

    def test_uv_install_runs_the_installers_upgrade_then_restarts(self, capsys):
        with (
            patch(f"{_UP}.installation", return_value=Installation("uv")),
            patch(f"{_UP}.installed_version", return_value="0.1.0"),
            patch(f"{_UP}.subprocess.run", return_value=MagicMock(returncode=0)) as run,
            patch(f"{_UP}._fresh_version", return_value="0.1.1"),
            patch(f"{_UP}.restart_backbone", new_callable=AsyncMock, return_value=0) as restart,
        ):
            assert _run(["upgrade"]) == 0
        assert run.call_args.args[0] == ["uv", "tool", "upgrade", "agent-backbone"]
        restart.assert_awaited_once()
        assert "installed: 0.1.1" in capsys.readouterr().out

    def test_failed_upgrade_does_not_restart(self, capsys):
        with (
            patch(f"{_UP}.installation", return_value=Installation("pipx")),
            patch(f"{_UP}.installed_version", return_value="0.1.0"),
            patch(f"{_UP}.subprocess.run", return_value=MagicMock(returncode=1)),
            patch(f"{_UP}.restart_backbone", new_callable=AsyncMock) as restart,
        ):
            assert _run(["upgrade"]) == 1
        restart.assert_not_called()
        assert "nothing was restarted" in capsys.readouterr().out

    def test_development_checkout_downloads_nothing_and_restarts(self, capsys):
        with (
            patch(f"{_UP}.installation", return_value=Installation("editable", "/ws/x")),
            patch(f"{_UP}.installed_version", return_value="0.1.0"),
            patch(f"{_UP}.subprocess.run") as run,
            patch(f"{_UP}.restart_backbone", new_callable=AsyncMock, return_value=0) as restart,
        ):
            assert _run(["upgrade"]) == 0
        run.assert_not_called()
        restart.assert_awaited_once()
        assert "development checkout" in capsys.readouterr().out

    def test_no_restart_flag(self, capsys):
        with (
            patch(f"{_UP}.installation", return_value=Installation("editable", "/ws/x")),
            patch(f"{_UP}.installed_version", return_value="0.1.0"),
            patch(f"{_UP}.restart_backbone", new_callable=AsyncMock) as restart,
        ):
            assert _run(["upgrade", "--no-restart"]) == 0
        restart.assert_not_called()
        assert "not restarting" in capsys.readouterr().out


class TestRestartBackbone:
    async def test_login_service_is_restarted_and_the_api_awaited(self, tmp_path, capsys):
        config = bootstrap_config(tmp_path / "data")
        with (
            patch("agent_backbone.cli.service.state", return_value="running"),
            patch("agent_backbone.cli.service.restart", return_value=0) as restart,
            patch(f"{_UP}._wait_for_api", new_callable=AsyncMock, return_value="0.1.1"),
        ):
            assert await upgrade.restart_backbone(config) == 0
        restart.assert_called_once()
        assert "back (version 0.1.1)" in capsys.readouterr().out

    async def test_tmux_session_is_cycled(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        with (
            patch("agent_backbone.cli.service.state", return_value="not installed"),
            patch(
                "agent_backbone.services.terminal.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("agent_backbone.cli.server._down", new_callable=AsyncMock) as down,
            patch(
                "agent_backbone.cli.server._up_detached", new_callable=AsyncMock, return_value=0
            ) as up,
            patch(f"{_UP}._wait_for_api", new_callable=AsyncMock, return_value="0.1.1"),
        ):
            assert await upgrade.restart_backbone(config) == 0
        down.assert_awaited_once()
        up.assert_awaited_once()

    async def test_nothing_running_is_not_an_error(self, tmp_path, capsys):
        config = bootstrap_config(tmp_path / "data")
        with (
            patch("agent_backbone.cli.service.state", return_value="not installed"),
            patch(
                "agent_backbone.services.terminal.session_exists",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("agent_backbone.cli._common.api_up", new_callable=AsyncMock, return_value=False),
        ):
            assert await upgrade.restart_backbone(config) == 0
        assert "not running" in capsys.readouterr().out

    async def test_api_not_back_in_time_is_reported(self, tmp_path, capsys):
        config = bootstrap_config(tmp_path / "data")
        with (
            patch("agent_backbone.cli.service.state", return_value="running"),
            patch("agent_backbone.cli.service.restart", return_value=0),
            patch(f"{_UP}._wait_for_api", new_callable=AsyncMock, return_value=None),
        ):
            assert await upgrade.restart_backbone(config) == 1
        assert "did not answer" in capsys.readouterr().out
