"""Tests for `backbone service` — the login service on macOS and Linux."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_backbone import cli
from agent_backbone.cli import service

_SVC = "agent_backbone.cli.service"


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return int(exc.value.code or 0)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path))
    yield tmp_path


def _ok(*args, **kwargs):
    return MagicMock(returncode=0, stdout="state = running", stderr="")


class TestMacOS:
    def test_install_writes_plist_and_bootstraps(self, tmp_path, capsys):
        with (
            patch(f"{_SVC}.platform.system", return_value="Darwin"),
            patch(f"{_SVC}.subprocess.run", side_effect=_ok) as run,
            patch(f"{_SVC}.shutil.which", return_value="/opt/bin/backbone"),
        ):
            assert _run(["service", "install"]) == 0
        plist = tmp_path / "Library" / "LaunchAgents" / "dev.agent-backbone.plist"
        text = plist.read_text()
        assert "<string>/opt/bin/backbone</string><string>up</string>" in text
        assert f"<string>{tmp_path / 'data'}</string>" in text
        assert "<key>KeepAlive</key><true/>" in text
        assert run.call_args.args[0][:2] == ("launchctl", "bootstrap")
        assert "starts at login" in capsys.readouterr().out

    def test_status_and_uninstall(self, tmp_path, capsys):
        with patch(f"{_SVC}.platform.system", return_value="Darwin"):
            assert service.state() == "not installed"
            plist = tmp_path / "Library" / "LaunchAgents" / "dev.agent-backbone.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text("<plist/>")
            with patch(f"{_SVC}.subprocess.run", side_effect=_ok):
                assert service.state() == "running"
                assert _run(["service", "uninstall"]) == 0
            assert not plist.exists()
            assert _run(["service", "status"]) == 0
        assert "not installed" in capsys.readouterr().out


class TestLinux:
    def test_install_writes_unit_and_enables(self, tmp_path):
        with (
            patch(f"{_SVC}.platform.system", return_value="Linux"),
            patch(f"{_SVC}.subprocess.run", side_effect=_ok) as run,
            patch(f"{_SVC}.shutil.which", return_value="/opt/bin/backbone"),
        ):
            assert _run(["service", "install"]) == 0
        unit = tmp_path / ".config" / "systemd" / "user" / "agent-backbone.service"
        text = unit.read_text()
        assert "ExecStart=/opt/bin/backbone up" in text
        assert f"Environment=BACKBONE_DATA_DIR={tmp_path / 'data'}" in text
        assert "Restart=always" in text
        calls = [c.args[0] for c in run.call_args_list]
        assert ("systemctl", "--user", "daemon-reload") in calls
        assert ("systemctl", "--user", "enable", "--now", "agent-backbone.service") in calls


def test_unsupported_platform():
    with patch(f"{_SVC}.platform.system", return_value="Windows"):
        assert service.install() == 1
        assert service.state() == "unsupported"


class TestValueEncoding:
    def test_plist_escapes_xml_special_characters(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", "/opt/a&b:/usr/bin")
        text = service._plist("/opt/<x>/backbone", tmp_path / "d&d", tmp_path / "l.log")
        assert "&lt;x&gt;" in text and "d&amp;d" in text and "a&amp;b" in text
        assert "<x>" not in text.split("ProgramArguments")[1].split("</array>")[0]

    def test_unit_quotes_paths_with_spaces(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", "/opt/my tools:/usr/bin")
        text = service._unit("/opt/my tools/backbone", tmp_path / "data dir")
        assert "ExecStart='/opt/my tools/backbone' up" in text
        assert f"Environment='BACKBONE_DATA_DIR={tmp_path / 'data dir'}'" in text
        assert "Environment='PATH=/opt/my tools:/usr/bin'" in text
