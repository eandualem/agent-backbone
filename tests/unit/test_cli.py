"""Tests for the `backbone` CLI."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone import cli


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return int(exc.value.code or 0)


class TestInit:
    def test_writes_config_and_env(self, tmp_path, capsys):
        assert _run(["init", "--dir", str(tmp_path)]) == 0
        toml = tmp_path / "backbone.toml"
        env = tmp_path / ".env"
        assert toml.is_file() and env.is_file()
        assert "[agents.reviewer]" in toml.read_text()
        assert "BACKBONE_API_KEY=" in env.read_text()
        assert len(env.read_text().split("BACKBONE_API_KEY=")[1].splitlines()[0]) >= 32
        assert oct(env.stat().st_mode)[-3:] == "600"

    def test_refuses_to_overwrite_without_force(self, tmp_path):
        (tmp_path / "backbone.toml").write_text("x")
        assert _run(["init", "--dir", str(tmp_path)]) == 1
        assert (tmp_path / "backbone.toml").read_text() == "x"
        assert _run(["init", "--dir", str(tmp_path), "--force"]) == 0


class TestDoctor:
    def test_reports_missing_pieces(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BACKBONE_CONFIG", str(tmp_path / "backbone.toml"))
        monkeypatch.delenv("BACKBONE_API_KEY", raising=False)
        (tmp_path / "backbone.toml").write_text(
            f'[backbone]\ndata_dir = "{tmp_path / "data"}"\n'
            f'[agents.a]\ndir = "{tmp_path / "missing"}"\n'
        )
        code = _run(["doctor"])
        out = capsys.readouterr().out
        assert code == 1
        assert "dir exists" in out and "✗" in out
        assert "API key configured" in out

    def test_passes_with_valid_setup(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BACKBONE_CONFIG", str(tmp_path / "backbone.toml"))
        monkeypatch.setenv("BACKBONE_API_KEY", "k")
        (tmp_path / "agent").mkdir()
        (tmp_path / "backbone.toml").write_text(
            f'[backbone]\ndata_dir = "{tmp_path / "data"}"\n'
            f'[agents.a]\ndir = "{tmp_path / "agent"}"\nruntime = "shell"\n'
        )
        with patch("agent_backbone.cli.shutil.which", return_value="/usr/bin/tmux"):
            code = _run(["doctor"])
        assert code == 0
        assert "All good" in capsys.readouterr().out


class TestAgentCommands:
    def test_list(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BACKBONE_CONFIG", str(tmp_path / "backbone.toml"))
        (tmp_path / "backbone.toml").write_text('[agents.a]\ndir = "/x"\n[agents.b]\ndir = "/y"\n')
        assert _run(["agent", "list"]) == 0
        out = capsys.readouterr().out
        assert "a" in out and "/y" in out

    def test_start_unknown_agent(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BACKBONE_CONFIG", str(tmp_path / "backbone.toml"))
        (tmp_path / "backbone.toml").write_text('[agents.a]\ndir = "/x"\n')
        assert _run(["agent", "start", "zzz"]) == 1
        assert "unknown agent" in capsys.readouterr().out

    def test_start_known_agent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKBONE_CONFIG", str(tmp_path / "backbone.toml"))
        (tmp_path / "backbone.toml").write_text('[agents.a]\ndir = "/x"\n')
        with patch(
            "agent_backbone.services.infrastructure._agents.start_agent",
            new_callable=AsyncMock,
            return_value=True,
        ) as start:
            assert _run(["agent", "start", "a", "--model", "m"]) == 0
        assert start.await_args.args[0].name == "a"
        assert start.await_args.kwargs["model"] == "m"


class TestTell:
    def test_posts_to_running_api(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BACKBONE_CONFIG", str(tmp_path / "backbone.toml"))
        monkeypatch.setenv("BACKBONE_API_KEY", "k")
        (tmp_path / "backbone.toml").write_text('[agents.a]\ndir = "/x"\n')

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True, "session": "a", "outcome": "delivered"}

        client = AsyncMock()
        client.post = AsyncMock(return_value=_Resp())
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=client):
            assert _run(["tell", "a", "hello", "world", "--from", "me"]) == 0

        payload = client.post.await_args.kwargs["json"]
        assert payload == {
            "target_session": "a",
            "from_entity": "me",
            "message": "hello world",
            "priority": False,
        }
        assert client.post.await_args.kwargs["headers"] == {"Authorization": "Bearer k"}
