"""Tests for runtime launch commands — hook injection at agent start."""

from __future__ import annotations

import json
from unittest.mock import patch

from agent_backbone.services.infrastructure._agents import build_command, hook_launch_args

_MOD = "agent_backbone.services.infrastructure._agents"


class TestHookLaunchArgs:
    def test_claude_gets_backbone_owned_settings(self, tmp_path):
        args = hook_launch_args("claude", tmp_path, tmp_path / "state")
        assert args[0] == "--settings"
        settings_path = tmp_path / "hooks" / "claude-settings.json"
        assert args[1] == str(settings_path)
        saved = json.loads(settings_path.read_text())
        assert "SessionStart" in saved["hooks"]

    def test_other_runtimes_get_no_args(self, tmp_path):
        assert hook_launch_args("codex", tmp_path, tmp_path / "state") == []
        assert hook_launch_args("shell", tmp_path, tmp_path / "state") == []

    def test_missing_dirs_get_no_args(self, tmp_path):
        assert hook_launch_args("claude", None, tmp_path) == []
        assert hook_launch_args("claude", tmp_path, None) == []

    def test_unwritable_data_dir_degrades_to_no_args(self, tmp_path):
        with patch(
            "agent_backbone.hooks.install.ensure_launch_settings",
            side_effect=OSError("read-only"),
        ):
            assert hook_launch_args("claude", tmp_path, tmp_path / "state") == []


class TestBuildCommand:
    def test_claude_command_includes_settings(self, tmp_path):
        with patch(f"{_MOD}.resolve_command", return_value="/usr/bin/claude"):
            command = build_command(
                "claude", model="opus", data_dir=tmp_path, state_dir=tmp_path / "state"
            )
        assert command[:3] == ["/usr/bin/claude", "--model", "opus"]
        assert command[3] == "--settings"
        assert command[4].endswith("claude-settings.json")

    def test_claude_without_dirs_has_no_settings(self):
        with patch(f"{_MOD}.resolve_command", return_value="/usr/bin/claude"):
            command = build_command("claude")
        assert command == ["/usr/bin/claude"]

    def test_shell_stays_none(self, tmp_path):
        assert build_command("shell", data_dir=tmp_path, state_dir=tmp_path) is None

    def test_codex_brief_becomes_initial_prompt(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent x.")
        with patch(f"{_MOD}.resolve_command", return_value="/bin/codex"):
            command = build_command("codex", model="gpt-5.2", system_prompt_file=brief)
        assert command == ["/bin/codex", "--model", "gpt-5.2", "You are agent x."]

    def test_codex_resume_is_a_subcommand(self):
        with patch(f"{_MOD}.resolve_command", return_value="/bin/codex"):
            assert build_command("codex", resume=True) == ["/bin/codex", "resume", "--last"]

    def test_codex_unreadable_brief_degrades(self, tmp_path):
        with patch(f"{_MOD}.resolve_command", return_value="/bin/codex"):
            command = build_command("codex", system_prompt_file=tmp_path / "missing.md")
        assert command == ["/bin/codex"]

    def test_gemini_flags(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent y.")
        with patch(f"{_MOD}.resolve_command", return_value="/bin/gemini"):
            command = build_command(
                "gemini", model="gemini-3-pro", pre_trust=True, system_prompt_file=brief
            )
        assert command == [
            "/bin/gemini",
            "--model",
            "gemini-3-pro",
            "--skip-trust",
            "--prompt-interactive",
            "You are agent y.",
        ]

    def test_gemini_resume_does_not_rebrief(self, tmp_path):
        # The resumed session already received the brief as its initial prompt.
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent y.")
        with patch(f"{_MOD}.resolve_command", return_value="/bin/gemini"):
            command = build_command("gemini", resume=True, system_prompt_file=brief)
        assert command == ["/bin/gemini", "--resume", "latest"]

    def test_gemini_without_pre_trust_keeps_dialog(self):
        with patch(f"{_MOD}.resolve_command", return_value="/bin/gemini"):
            assert build_command("gemini") == ["/bin/gemini"]

    def test_opencode_flags(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent z.")
        with patch(f"{_MOD}.resolve_command", return_value="/bin/opencode"):
            command = build_command(
                "opencode", model="opencode/big-pickle", system_prompt_file=brief
            )
        assert command == [
            "/bin/opencode",
            "--model",
            "opencode/big-pickle",
            "--prompt",
            "You are agent z.",
        ]

    def test_opencode_resume_does_not_rebrief(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent z.")
        with patch(f"{_MOD}.resolve_command", return_value="/bin/opencode"):
            command = build_command("opencode", resume=True, system_prompt_file=brief)
        assert command == ["/bin/opencode", "--continue"]


class TestPreTrust:
    def test_writes_trust_record_preserving_existing_state(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_directory

        config = tmp_path / "claude.json"
        config.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "projects": {"/existing": {"hasTrustDialogAccepted": True, "lastCost": 1}},
                }
            )
        )
        project = tmp_path / "proj"
        project.mkdir()

        assert pre_trust_directory(project, claude_config=config) is True

        saved = json.loads(config.read_text())
        assert saved["theme"] == "dark"
        assert saved["projects"]["/existing"]["lastCost"] == 1
        assert saved["projects"][str(project)]["hasTrustDialogAccepted"] is True

    def test_creates_config_when_missing(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_directory

        config = tmp_path / "claude.json"
        assert pre_trust_directory(tmp_path / "p", claude_config=config) is True
        saved = json.loads(config.read_text())
        assert saved["projects"][str(tmp_path / "p")]["hasTrustDialogAccepted"] is True

    def test_already_trusted_is_a_noop(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_directory

        config = tmp_path / "claude.json"
        project = tmp_path / "p"
        assert pre_trust_directory(project, claude_config=config) is True
        before = config.read_text()
        assert pre_trust_directory(project, claude_config=config) is True
        assert config.read_text() == before

    def test_corrupt_config_fails_softly(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_directory

        config = tmp_path / "claude.json"
        config.write_text("{broken")
        assert pre_trust_directory(tmp_path / "p", claude_config=config) is False
        assert config.read_text() == "{broken"


class TestPreTrustRuntime:
    def test_dispatches_per_runtime(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_runtime

        with (
            patch(f"{_MOD}.pre_trust_directory") as claude,
            patch(f"{_MOD}.pre_trust_codex_directory") as codex,
        ):
            pre_trust_runtime("claude", tmp_path)
            pre_trust_runtime("codex", tmp_path)
            pre_trust_runtime("gemini", tmp_path)  # --skip-trust at launch instead
            pre_trust_runtime("opencode", tmp_path)  # no trust dialog
        claude.assert_called_once_with(tmp_path)
        codex.assert_called_once_with(tmp_path)


class TestPreTrustCodex:
    def test_scalar_projects_value_fails_softly(self, tmp_path):
        # Valid TOML, unexpected shape: never raise, never rewrite the user's file.
        from agent_backbone.services.infrastructure._agents import pre_trust_codex_directory

        config = tmp_path / "config.toml"
        config.write_text("projects = 1\n")
        assert pre_trust_codex_directory(tmp_path / "p", codex_config=config) is False
        assert config.read_text() == "projects = 1\n"

    def test_scalar_project_entry_is_left_alone(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_codex_directory

        project = tmp_path / "p"
        config = tmp_path / "config.toml"
        config.write_text(f'[projects]\n"{project}" = "weird"\n')
        assert pre_trust_codex_directory(project, codex_config=config) is False
        assert "weird" in config.read_text()

    def test_leaves_no_temp_file_behind(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_codex_directory

        config = tmp_path / "config.toml"
        assert pre_trust_codex_directory(tmp_path / "p", codex_config=config) is True
        assert [p.name for p in tmp_path.iterdir() if p.name != "config.toml"] == []

    def test_appends_trust_record_preserving_config(self, tmp_path):
        import tomllib

        from agent_backbone.services.infrastructure._agents import pre_trust_codex_directory

        config = tmp_path / "config.toml"
        config.write_text('model = "gpt-5.2"\n\n[projects."/existing"]\ntrust_level = "trusted"\n')
        project = tmp_path / "proj"
        project.mkdir()

        assert pre_trust_codex_directory(project, codex_config=config) is True
        saved = tomllib.loads(config.read_text())
        assert saved["model"] == "gpt-5.2"
        assert saved["projects"]["/existing"]["trust_level"] == "trusted"
        assert saved["projects"][str(project)]["trust_level"] == "trusted"

    def test_creates_config_when_missing(self, tmp_path):
        import tomllib

        from agent_backbone.services.infrastructure._agents import pre_trust_codex_directory

        config = tmp_path / "config.toml"
        assert pre_trust_codex_directory(tmp_path / "p", codex_config=config) is True
        saved = tomllib.loads(config.read_text())
        assert saved["projects"][str(tmp_path / "p")]["trust_level"] == "trusted"

    def test_existing_user_decision_is_left_alone(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_codex_directory

        project = tmp_path / "p"
        project.mkdir()
        config = tmp_path / "config.toml"
        config.write_text(f'[projects."{project}"]\ntrust_level = "untrusted"\n')
        before = config.read_text()
        assert pre_trust_codex_directory(project, codex_config=config) is False
        assert config.read_text() == before

    def test_corrupt_config_fails_softly(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import pre_trust_codex_directory

        config = tmp_path / "config.toml"
        config.write_text("model = [broken")
        assert pre_trust_codex_directory(tmp_path / "p", codex_config=config) is False
        assert config.read_text() == "model = [broken"


class TestApproveAgent:
    DIALOG = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
    IDLE = "❯ \n  ? for shortcuts\n"

    async def test_answers_only_a_visible_prompt(self):
        from agent_backbone.services.infrastructure._agents import approve_agent

        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", side_effect=[self.DIALOG, self.IDLE]),
            patch(
                "agent_backbone.services.terminal._adapters.send_keys", return_value=True
            ) as keys,
        ):
            outcome, evidence = await approve_agent("ike", runtime="claude", settle_seconds=0)
        assert outcome == "approved"
        assert keys.await_args.args == ("ike", "Enter")
        assert evidence[0] == "answered with Enter; prompt cleared"
        assert "Do you want to proceed?" in evidence  # the dialog is quoted for the audit

    async def test_idle_prompt_is_never_typed_into(self):
        from agent_backbone.services.infrastructure._agents import approve_agent

        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value=self.IDLE),
            patch("agent_backbone.services.terminal._adapters.send_keys") as keys,
        ):
            outcome, evidence = await approve_agent("ike", runtime="claude", settle_seconds=0)
        assert outcome == "not_waiting"
        assert evidence[0].startswith("terminal shows no active permission prompt")
        keys.assert_not_called()

    async def test_unknown_answer_sequence_is_refused(self):
        from agent_backbone.services.infrastructure._agents import approve_agent

        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value="$ "),
            patch("agent_backbone.services.terminal._adapters.send_keys") as keys,
        ):
            outcome, _ = await approve_agent("ike", runtime="shell", settle_seconds=0)
        assert outcome == "unsupported"
        keys.assert_not_awaited()

    async def test_offline(self):
        from agent_backbone.services.infrastructure._agents import approve_agent

        with patch(f"{_MOD}.session_exists", return_value=False):
            assert (await approve_agent("ike", runtime="claude"))[0] == "offline"

    async def test_stale_dialog_above_an_idle_prompt_is_not_answered(self):
        # The dialog text is still on screen but the runtime is back at its
        # prompt with typed text — Enter would submit that text.
        from agent_backbone.services.infrastructure._agents import approve_agent

        stale = self.DIALOG + "❯ rm -rf build\n  ? for shortcuts\n"
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value=stale),
            patch("agent_backbone.services.terminal._adapters.send_keys") as keys,
        ):
            outcome, _ = await approve_agent("ike", runtime="claude", settle_seconds=0)
        assert outcome == "not_waiting"
        keys.assert_not_called()


class TestStartAgentScrubsSecrets:
    """A started agent inherits the launch contract and nothing else (issue #81)."""

    async def test_secrets_are_scrubbed_from_the_session(self, tmp_path):
        from unittest.mock import AsyncMock

        from agent_backbone.config import AgentSpec, bootstrap_config
        from agent_backbone.services.infrastructure._agents import start_agent

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / ".env").write_text("BACKBONE_API_KEY=k\nMY_OWN_SECRET=s\n")
        project = tmp_path / "project"
        project.mkdir()
        spec = AgentSpec(name="ike", dir=str(project), runtime="shell")

        config = bootstrap_config(data_dir)
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
        ):
            assert (await start_agent(spec, config, wait=False)).ok is True

        scrub = start.await_args.kwargs["scrub"]
        assert "BACKBONE_API_KEY" in scrub
        assert "GITHUB_TOKEN" in scrub  # a known name, even absent from .env
        assert "MY_OWN_SECRET" in scrub  # whatever the user put in .env
        # The launch contract itself is untouched.
        assert start.await_args.kwargs["environment"]["BACKBONE_AGENT"] == "ike"


class TestStartAgentBrief:
    """One launch path: the brief reaches every runtime, at launch or as a message."""

    @staticmethod
    def _spec(tmp_path, runtime):
        from agent_backbone.config import AgentSpec

        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        return AgentSpec(name="ike", dir=str(project), runtime=runtime, repo="acme/app")

    @staticmethod
    def _launch(tmp_path):
        from unittest.mock import AsyncMock

        return (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True),
            patch(f"{_MOD}.resolve_command", return_value="/bin/x"),
            patch(f"{_MOD}.pre_trust_runtime"),
            patch(f"{_MOD}.wait_until_ready", new_callable=AsyncMock, return_value=("ready", [])),
            patch("agent_backbone.services.routing.safe_deliver", new_callable=AsyncMock),
        )

    async def test_claude_gets_the_brief_at_launch(self, tmp_path):
        from agent_backbone.config import bootstrap_config
        from agent_backbone.services.infrastructure._agents import start_agent

        config = bootstrap_config(tmp_path / "data")
        exists, start, _cmd, _trust, _wait, deliver = self._launch(tmp_path)
        with exists, start as started, _cmd, _trust, _wait, deliver as delivered:
            result = await start_agent(self._spec(tmp_path, "claude"), config)
        assert result.ok and result.ready == "ready"
        command = started.await_args.kwargs["command"]
        assert "--append-system-prompt-file" in command
        delivered.assert_not_awaited()

    async def test_aider_gets_the_brief_as_its_first_message(self, tmp_path):
        from agent_backbone.config import bootstrap_config
        from agent_backbone.services.infrastructure._agents import start_agent

        config = bootstrap_config(tmp_path / "data")
        exists, start, _cmd, _trust, _wait, deliver = self._launch(tmp_path)
        with exists, start as started, _cmd, _trust, _wait, deliver as delivered:
            result = await start_agent(self._spec(tmp_path, "aider"), config)
        assert result.ok
        assert "--append-system-prompt-file" not in started.await_args.kwargs["command"]
        delivered.assert_awaited_once()
        assert delivered.await_args.args[0] == "ike"
        assert delivered.await_args.args[1].startswith("[via:backbone] ")
        assert delivered.await_args.kwargs["flow_name"] == "agent-brief"

    async def test_a_swarm_role_brief_replaces_the_common_brief(self, tmp_path):
        from agent_backbone.config import bootstrap_config
        from agent_backbone.services.infrastructure._agents import start_agent

        config = bootstrap_config(tmp_path / "data")
        role = tmp_path / "role.md"
        role.write_text("You are the scout.")
        exists, start, _cmd, _trust, _wait, deliver = self._launch(tmp_path)
        with exists, start, _cmd, _trust, _wait, deliver as delivered:
            await start_agent(self._spec(tmp_path, "aider"), config, brief_file=role)
        assert delivered.await_args.args[1] == "[via:backbone] You are the scout."

    async def test_shell_and_resume_get_no_brief(self, tmp_path):
        from agent_backbone.config import bootstrap_config
        from agent_backbone.services.infrastructure._agents import start_agent

        config = bootstrap_config(tmp_path / "data")
        exists, start, _cmd, _trust, _wait, deliver = self._launch(tmp_path)
        with exists, start, _cmd, _trust, _wait, deliver as delivered:
            await start_agent(self._spec(tmp_path, "shell"), config)
            await start_agent(self._spec(tmp_path, "aider"), config, resume=True)
        delivered.assert_not_awaited()
