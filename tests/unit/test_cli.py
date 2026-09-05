"""Tests for the `backbone` CLI (database-backed configuration)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone import cli
from agent_backbone.cli import setup
from agent_backbone.services.agents import AgentState, StartResult, StateSnapshot

_DETECT_REPO = "agent_backbone.services.agents.store.detect_repo"


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return int(exc.value.code or 0)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("BACKBONE_DATABASE_URL", raising=False)
    monkeypatch.delenv("BACKBONE_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("BACKBONE_AGENT", raising=False)
    # Never talk to a real backbone during tests
    with patch("agent_backbone.cli._common.api_up", new_callable=AsyncMock, return_value=False):
        yield tmp_path / "data"


class TestInit:
    def test_creates_data_dir_env_and_database(self, _isolated_data_dir, capsys):
        data = _isolated_data_dir
        assert _run(["init"]) == 0
        env = data / ".env"
        assert env.is_file() and (data / "backbone.db").is_file()
        assert "BACKBONE_API_KEY=" in env.read_text()
        assert len(env.read_text().split("BACKBONE_API_KEY=")[1].splitlines()[0]) >= 32
        assert oct(env.stat().st_mode)[-3:] == "600"
        assert "backbone agent start" in capsys.readouterr().out

    def test_keeps_env_without_force(self, _isolated_data_dir):
        data = _isolated_data_dir
        data.mkdir(parents=True)
        (data / ".env").write_text("BACKBONE_API_KEY=keep\n")
        assert _run(["init"]) == 0
        assert (data / ".env").read_text() == "BACKBONE_API_KEY=keep\n"
        assert _run(["init", "--force"]) == 0
        assert "keep" not in (data / ".env").read_text()

    def test_custom_data_dir_prints_export_hint(self, tmp_path, capsys):
        assert _run(["init", "--data-dir", str(tmp_path / "custom")]) == 0
        out = capsys.readouterr().out
        assert "export BACKBONE_DATA_DIR=" in out and str(tmp_path / "custom") in out

    def test_default_init_prints_no_export_hint(self, _isolated_data_dir, capsys):
        assert _run(["init"]) == 0
        assert "export BACKBONE_DATA_DIR" not in capsys.readouterr().out


class TestConfig:
    def test_set_get_list_unset(self, capsys):
        assert _run(["init"]) == 0
        capsys.readouterr()
        assert _run(["config", "set", "backbone.port", "7999"]) == 0
        capsys.readouterr()
        assert _run(["config", "get", "backbone.port"]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "7999"
        assert _run(["config", "list"]) == 0
        out = capsys.readouterr().out
        assert "* backbone.port" in out and "timing.grace_period_seconds" in out
        assert _run(["config", "unset", "backbone.port"]) == 0
        capsys.readouterr()
        assert _run(["config", "get", "backbone.port"]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "7120"

    def test_rejects_bad_values(self, capsys):
        assert _run(["init"]) == 0
        assert _run(["config", "set", "backbone.port", "lots"]) == 1
        assert _run(["config", "set", "nope.key", "1"]) == 1

    def test_unset_reports_api_failure_instead_of_claiming_success(self, capsys):
        assert _run(["init"]) == 0
        capsys.readouterr()
        with (
            patch("agent_backbone.cli._common.api_up", new_callable=AsyncMock, return_value=True),
            patch(
                "agent_backbone.cli._common.api",
                new_callable=AsyncMock,
                return_value=(404, {"detail": "nope"}),
            ),
        ):
            assert _run(["config", "unset", "backbone.port"]) == 1
        assert "API error" in capsys.readouterr().out


class TestDoctor:
    def test_reports_missing_pieces(self, tmp_path, capsys):
        assert _run(["init"]) == 0
        assert _run(["agent", "set", "ghost", "dir=/nope"]) == 1  # unknown agent
        with patch("agent_backbone.cli.setup.shutil.which", return_value=None):
            code = _run(["doctor"])
        out = capsys.readouterr().out
        assert code == 1
        assert "tmux on PATH" in out and "✗" in out

    def test_passes_with_valid_setup(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BACKBONE_API_KEY", "k")
        assert _run(["init"]) == 0
        with patch("agent_backbone.cli.setup.shutil.which", return_value="/usr/bin/tmux"):
            code = _run(["doctor"])
        assert code == 0
        assert "All good" in capsys.readouterr().out


class TestAgentCommands:
    @pytest.fixture(autouse=True)
    def _runtimes_installed(self):
        # CI runners have no claude binary; the launch itself is patched per test.
        with patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=True):
            yield

    def test_start_discovers_agent_from_directory(self, tmp_path, capsys):
        assert _run(["init"]) == 0
        project = tmp_path / "my-app"
        project.mkdir()
        with (
            patch(
                "agent_backbone.services.agents.launch.start_agent",
                new_callable=AsyncMock,
                return_value=StartResult(
                    ok=True, ready="ready", evidence=("terminal shows an empty prompt",)
                ),
            ) as start,
            patch(
                _DETECT_REPO,
                new_callable=AsyncMock,
                return_value="acme/my-app",
            ),
        ):
            assert _run(["agent", "start", "--dir", str(project), "--runtime", "shell"]) == 0
        out = capsys.readouterr().out
        assert "my-app: ready" in out and "acme/my-app" in out
        assert start.await_args.args[0].name == "my-app"
        assert start.await_args.args[0].repo == "acme/my-app"

        assert _run(["agent", "list"]) == 0
        out = capsys.readouterr().out
        assert "my-app" in out and str(project) in out

    def test_start_unknown_name_registers_cwd(self, tmp_path, monkeypatch, capsys):
        assert _run(["init"]) == 0
        project = tmp_path / "some-project"
        project.mkdir()
        monkeypatch.chdir(project)
        with (
            patch(
                "agent_backbone.services.agents.launch.start_agent",
                new_callable=AsyncMock,
                return_value=StartResult(ok=True),
            ) as start,
            patch(
                _DETECT_REPO,
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            assert _run(["agent", "start", "orch", "--no-wait"]) == 0
        out = capsys.readouterr().out
        assert "'orch' is new" in out
        assert start.await_args.args[0].name == "orch"
        assert start.await_args.args[0].path == project

    def test_start_override_updates_recorded_runtime_and_model(self, tmp_path, capsys):
        assert _run(["init"]) == 0
        project = tmp_path / "desk"
        project.mkdir()
        with (
            patch(
                "agent_backbone.services.agents.launch.start_agent",
                new_callable=AsyncMock,
                return_value=StartResult(ok=True),
            ) as start,
            patch(
                _DETECT_REPO,
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            assert _run(["agent", "start", "--dir", str(project), "--no-wait"]) == 0
            assert _run(["agent", "start", "desk", "--model", "opus", "--no-wait"]) == 0
        assert start.await_args.args[0].model == "opus"
        capsys.readouterr()
        assert _run(["agent", "list"]) == 0  # persisted, not a one-off
        # A later bare start must reuse the recorded model.
        with (
            patch(
                "agent_backbone.services.agents.launch.start_agent",
                new_callable=AsyncMock,
                return_value=StartResult(ok=True),
            ) as start,
            patch(
                _DETECT_REPO,
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            assert _run(["agent", "start", "desk", "--no-wait"]) == 0
        assert start.await_args.args[0].model == "opus"

    def test_group_start_requires_known_names(self, capsys):
        assert _run(["init"]) == 0
        assert _run(["agent", "start", "zzz", "yyy"]) == 1
        assert "unknown agent" in capsys.readouterr().out

    def test_direct_stop_refuses_backbone_session(self, capsys):
        # The shared operation stops through the launch module: patch what it calls.
        with patch(
            "agent_backbone.services.agents.operations.launch.stop_agent", new_callable=AsyncMock
        ) as stop:
            assert _run(["agent", "stop", "backbone"]) == 1
        stop.assert_not_awaited()
        assert "refusing to stop" in capsys.readouterr().out

    def test_direct_stop_stops_an_agent_through_the_shared_operation(self, capsys):
        with patch(
            "agent_backbone.services.agents.operations.launch.stop_agent",
            new_callable=AsyncMock,
            return_value=True,
        ) as stop:
            assert _run(["agent", "stop", "orch"]) == 0
        stop.assert_awaited_once_with("orch")
        assert "orch: stopped" in capsys.readouterr().out

    def test_direct_inspect_uses_configured_state_helper(self, capsys):
        snapshot = StateSnapshot(state=AgentState.BUSY, evidence=["configured runtime"])
        with (
            patch(
                "agent_backbone.services.agents.agent_state",
                new_callable=AsyncMock,
                return_value=snapshot,
            ) as state,
            patch(
                "agent_backbone.services.terminal.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            assert _run(["agent", "inspect", "ike"]) == 0

        assert state.await_args.args[1] == "ike"
        assert "configured runtime" in capsys.readouterr().out

    def test_moved_directory_follows_and_same_name_gets_suffix(self, tmp_path, capsys):
        assert _run(["init"]) == 0
        old = tmp_path / "projects" / "app"
        old.mkdir(parents=True)
        with (
            patch(
                "agent_backbone.services.agents.launch.start_agent",
                new_callable=AsyncMock,
                return_value=StartResult(ok=True),
            ) as start,
            patch(
                _DETECT_REPO,
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            assert _run(["agent", "start", "--dir", str(old), "--no-wait"]) == 0

            # Same folder name while the old directory still exists: a
            # different project, registered with a numbered name.
            twin = tmp_path / "elsewhere" / "app"
            twin.mkdir(parents=True)
            assert _run(["agent", "start", "--dir", str(twin), "--no-wait"]) == 0
            assert start.await_args.args[0].name == "app-2"

            # The old directory is gone: the record follows the move.
            moved = tmp_path / "moved-app"
            old.rename(moved)
            new_home = tmp_path / "workspace" / "app"
            new_home.mkdir(parents=True)
            assert _run(["agent", "start", "--dir", str(new_home), "--no-wait"]) == 0
            assert start.await_args.args[0].name == "app"
            assert start.await_args.args[0].path == new_home
        capsys.readouterr()
        assert _run(["agent", "list"]) == 0
        out = capsys.readouterr().out
        assert str(new_home) in out and str(twin) in out

    def test_set_watch_forget(self, tmp_path, capsys):
        assert _run(["init"]) == 0
        project = tmp_path / "orch"
        project.mkdir()
        with (
            patch(
                "agent_backbone.services.agents.launch.start_agent",
                new_callable=AsyncMock,
                return_value=StartResult(ok=True),
            ),
            patch(
                _DETECT_REPO,
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            assert _run(["agent", "start", "--dir", str(project), "--no-wait"]) == 0
        assert _run(["agent", "watch", "orch", "acme/app", "acme/web"]) == 0
        assert _run(["agent", "set", "orch", "description=Coordinates", "repo=acme/orch"]) == 0
        capsys.readouterr()
        assert _run(["agent", "list"]) == 0
        assert "orch" in capsys.readouterr().out
        assert _run(["agent", "unwatch", "orch", "acme/web"]) == 0
        with patch(
            "agent_backbone.services.agents.operations.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            assert _run(["agent", "forget", "orch"]) == 0
            assert _run(["agent", "forget", "orch"]) == 1

    def test_watch_defaults_to_own_session(self, tmp_path, monkeypatch, capsys):
        """Inside an agent session, the agent can watch repos without naming itself."""
        assert _run(["init"]) == 0
        project = tmp_path / "orch"
        project.mkdir()
        with (
            patch(
                "agent_backbone.services.agents.launch.start_agent",
                new_callable=AsyncMock,
                return_value=StartResult(ok=True),
            ),
            patch(
                _DETECT_REPO,
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            assert _run(["agent", "start", "--dir", str(project), "--no-wait"]) == 0
        capsys.readouterr()

        monkeypatch.setenv("BACKBONE_AGENT", "orch")
        assert _run(["agent", "watch", "acme/app", "acme/web"]) == 0
        out = capsys.readouterr().out
        assert "orch: now watching acme/app" in out and "acme/web" in out
        assert _run(["agent", "unwatch", "acme/web"]) == 0

        monkeypatch.delenv("BACKBONE_AGENT")
        assert _run(["agent", "watch", "acme/app"]) == 1
        assert "$BACKBONE_AGENT" in capsys.readouterr().out


class TestTell:
    def test_sender_defaults_to_backbone_agent(self, monkeypatch):
        monkeypatch.setenv("BACKBONE_AGENT", "orch")
        args = cli.build_parser().parse_args(["tell", "x", "hi"])
        assert args.sender == "orch"

    def test_posts_to_running_api(self, monkeypatch, capsys):
        monkeypatch.setenv("BACKBONE_API_KEY", "k")
        assert _run(["init"]) == 0
        capsys.readouterr()

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True, "session": "a", "outcome": "delivered"}

        client = AsyncMock()
        client.request = AsyncMock(return_value=_Resp())
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=client):
            assert _run(["tell", "a", "hello", "world", "--from", "me"]) == 0

        kwargs = client.request.await_args.kwargs
        assert kwargs["json"] == {
            "target_session": "a",
            "from_entity": "me",
            "message": "hello world",
            "priority": False,
        }
        assert kwargs["headers"] == {"Authorization": "Bearer k"}
        assert json.loads(capsys.readouterr().out)["ok"] is True

    def test_malformed_200_payload_is_an_error_not_a_traceback(self, monkeypatch, capsys):
        monkeypatch.setenv("BACKBONE_API_KEY", "k")
        assert _run(["init"]) == 0
        capsys.readouterr()

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return ["ok", True]  # a proxy page massaged into JSON

        client = AsyncMock()
        client.request = AsyncMock(return_value=_Resp())
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=client):
            assert _run(["tell", "a", "hello"]) == 1
        assert "unexpected response" in capsys.readouterr().out


class TestSwarmList:
    def test_malformed_200_items_are_an_error(self, capsys):
        assert _run(["init"]) == 0
        capsys.readouterr()
        with patch(
            "agent_backbone.cli._common.api",
            new_callable=AsyncMock,
            return_value=(200, {"items": "not-a-list"}),
        ):
            assert _run(["swarm", "list"]) == 1
        assert "unexpected swarm list" in capsys.readouterr().out

    def test_partial_entries_render_without_traceback(self, capsys):
        assert _run(["init"]) == 0
        capsys.readouterr()
        with patch(
            "agent_backbone.cli._common.api",
            new_callable=AsyncMock,
            return_value=(200, {"items": [{"name": "s1"}]}),
        ):
            assert _run(["swarm", "list"]) == 0
        assert "s1" in capsys.readouterr().out


class TestDown:
    async def test_failed_stop_is_reported(self, tmp_path, capsys):
        from agent_backbone.cli import server
        from agent_backbone.config import bootstrap_config

        config = bootstrap_config(tmp_path / "data")
        with (
            patch(
                "agent_backbone.services.terminal.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal.graceful_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            assert await server._down(config) == 1
        assert "failed to stop" in capsys.readouterr().out

    async def test_clean_stop(self, tmp_path, capsys):
        from agent_backbone.cli import server
        from agent_backbone.config import bootstrap_config

        config = bootstrap_config(tmp_path / "data")
        with (
            patch(
                "agent_backbone.services.terminal.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal.graceful_close",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            assert await server._down(config) == 0
        assert "backbone stopped" in capsys.readouterr().out


class TestHooks:
    def test_install_claude_into_project(self, tmp_path, _isolated_data_dir, capsys):
        project = tmp_path / "proj"
        assert _run(["hooks", "install", "claude", "--dir", str(project)]) == 0
        out = capsys.readouterr().out
        assert "installed Claude Code hooks" in out
        settings = project / ".claude" / "settings.json"
        assert settings.is_file()
        assert (_isolated_data_dir / "hooks" / "claude_hook.py").is_file()
        assert _run(["hooks", "uninstall", "claude", "--dir", str(project)]) == 0


class TestAgentApproveParser:
    def test_parses_name_and_sender(self, monkeypatch):
        from agent_backbone.cli import build_parser

        monkeypatch.setenv("BACKBONE_AGENT", "orch")
        ns = build_parser().parse_args(["agent", "approve", "scout"])
        assert ns.agent_command == "approve" and ns.name == "scout" and ns.sender == "orch"
        ns = build_parser().parse_args(["agent", "approve", "scout", "--from", "elias"])
        assert ns.sender == "elias"

    def test_deny_mirrors_approve(self, monkeypatch):
        from agent_backbone.cli import build_parser

        monkeypatch.setenv("BACKBONE_AGENT", "orch")
        ns = build_parser().parse_args(["agent", "deny", "scout"])
        assert ns.agent_command == "deny" and ns.name == "scout" and ns.sender == "orch"


class TestSecrets:
    def test_set_fills_the_init_placeholder_and_keeps_0600(self, _isolated_data_dir, capsys):
        assert _run(["init"]) == 0
        env_path = _isolated_data_dir / ".env"
        assert "# TELEGRAM_TOKEN=" in env_path.read_text()
        assert _run(["secrets", "set", "telegram_token", "123:abc"]) == 0
        text = env_path.read_text()
        assert "TELEGRAM_TOKEN=123:abc" in text and "# TELEGRAM_TOKEN=" not in text
        assert text.count("TELEGRAM_TOKEN=") == 1
        assert oct(env_path.stat().st_mode & 0o777) == "0o600"
        assert "added TELEGRAM_TOKEN" in capsys.readouterr().out
        # replacing keeps a single line
        assert _run(["secrets", "set", "TELEGRAM_TOKEN", "999:zzz"]) == 0
        assert env_path.read_text().count("TELEGRAM_TOKEN=") == 1
        assert "999:zzz" in env_path.read_text()

    def test_set_prompts_when_value_omitted(self, _isolated_data_dir, monkeypatch):
        assert _run(["init"]) == 0
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "secret-from-prompt")
        assert _run(["secrets", "set", "GITHUB_TOKEN"]) == 0
        assert "GITHUB_TOKEN=secret-from-prompt" in (_isolated_data_dir / ".env").read_text()

    def test_list_unset_and_path(self, _isolated_data_dir, capsys):
        assert _run(["init"]) == 0
        _run(["secrets", "set", "GITHUB_TOKEN", "x"])
        capsys.readouterr()
        assert _run(["secrets", "list"]) == 0
        out = capsys.readouterr().out
        assert "✓ BACKBONE_API_KEY" in out and "✓ GITHUB_TOKEN" in out and "- TELEGRAM_TOKEN" in out
        assert _run(["secrets", "unset", "GITHUB_TOKEN"]) == 0
        assert "GITHUB_TOKEN=" not in (_isolated_data_dir / ".env").read_text()
        assert _run(["secrets", "path"]) == 0
        assert capsys.readouterr().out.strip().endswith(str(_isolated_data_dir / ".env"))

    def test_rejects_bad_key_and_empty_value(self, _isolated_data_dir, capsys):
        assert _run(["init"]) == 0
        assert _run(["secrets", "set", "bad key", "x"]) == 1
        assert _run(["secrets", "set", "GITHUB_TOKEN", "   "]) == 1

    def test_set_reads_the_value_from_stdin_when_piped(self, _isolated_data_dir, monkeypatch):
        import io

        assert _run(["init"]) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO("piped-token\n"))
        assert _run(["secrets", "set", "GITHUB_TOKEN"]) == 0
        assert "GITHUB_TOKEN=piped-token" in (_isolated_data_dir / ".env").read_text()

    def test_set_and_unset_drop_duplicate_live_lines(self, _isolated_data_dir):
        # A hand-edited .env with two live lines for one key: the second
        # line must not survive to shadow the change.
        assert _run(["init"]) == 0
        env_path = _isolated_data_dir / ".env"
        env_path.write_text("GITHUB_TOKEN=old1\nGITHUB_TOKEN=old2\n")
        assert _run(["secrets", "set", "GITHUB_TOKEN", "new"]) == 0
        assert env_path.read_text() == "GITHUB_TOKEN=new\n"
        env_path.write_text("GITHUB_TOKEN=a\nGITHUB_TOKEN=b\n")
        assert _run(["secrets", "unset", "GITHUB_TOKEN"]) == 0
        assert "GITHUB_TOKEN=" not in env_path.read_text()


class TestApiClient:
    def test_loopback_is_http_and_anything_else_is_https(self):
        from dataclasses import replace

        from agent_backbone.cli import _common
        from agent_backbone.config import BackboneSection, bootstrap_config

        config = bootstrap_config()
        assert _common.api_url(config, "/health").startswith("http://127.0.0.1:")
        remote = replace(config, backbone=BackboneSection(host="backbone.internal", port=443))
        assert _common.api_url(remote, "/health") == "https://backbone.internal:443/health"

    async def test_non_json_body_is_wrapped(self, monkeypatch):
        import httpx

        from agent_backbone.cli import _common
        from agent_backbone.config import bootstrap_config

        class _Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def request(self, *args, **kwargs):
                return httpx.Response(502, text="Bad Gateway")

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        assert await _common.api(bootstrap_config(), "GET", "/x") == (
            502,
            {"detail": "Bad Gateway"},
        )


class TestRuntimesCommand:
    def test_lists_every_runtime_with_models(self, capsys):
        with patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=True):
            assert _run(["runtimes"]) == 0
        out = capsys.readouterr().out
        assert "deepcode" in out and "deepseek-v4-flash" in out
        assert "claude" in out and "opus, sonnet, haiku" in out
        assert "passed to the CLI verbatim" in out


class TestAlwaysOnStart:
    def test_without_always_on_agents_it_says_so(self, capsys):
        assert _run(["agent", "start", "--always-on"]) == 0
        assert "no always_on agents" in capsys.readouterr().out

    def test_names_and_always_on_do_not_mix(self, capsys):
        assert _run(["agent", "start", "app", "--always-on"]) == 1
        assert "do not pass names" in capsys.readouterr().out

    def test_helper_lists_the_marked_agents(self, tmp_path):
        from dataclasses import replace

        from agent_backbone.cli.agents import always_on_names
        from agent_backbone.config import AgentsConfig
        from tests.conftest import make_config

        config = make_config(tmp_path)
        marked = replace(config.agents.get("ike"), always_on=True)
        config = replace(config, agents=AgentsConfig({**config.agents.specs, "ike": marked}))
        assert always_on_names(config) == ["ike"]

    def test_always_on_with_resume_resumes_every_marked_agent(self, tmp_path, capsys):
        from agent_backbone.config import AgentSpec
        from agent_backbone.services.agents import StartResult

        seen: list[tuple[str, bool]] = []

        async def _resolve(store, req):
            return AgentSpec(name=req.name, dir=str(tmp_path), runtime="shell")

        async def _start(store, config, spec, req, *, db):
            seen.append((spec.name, req.resume))
            return StartResult(ok=True, ready="not_waited")

        with (
            patch("agent_backbone.cli.agents.always_on_names", return_value=["app", "web"]),
            patch("agent_backbone.services.agents.operations.resolve_agent", side_effect=_resolve),
            patch("agent_backbone.services.agents.operations.start_resolved", side_effect=_start),
        ):
            assert _run(["agent", "start", "--always-on", "--resume", "--no-wait"]) == 0
        assert seen == [("app", True), ("web", True)]
        assert "starting always_on agents: app, web" in capsys.readouterr().out
