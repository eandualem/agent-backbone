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
