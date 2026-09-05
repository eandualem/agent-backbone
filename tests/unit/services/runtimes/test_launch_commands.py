"""Tests for the launch side of each runtime: command, hooks, trust dialogs."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone.services.runtimes import RUNTIMES, split_model_effort
from agent_backbone.services.runtimes.claude import pre_trust_directory
from agent_backbone.services.runtimes.codex import pre_trust_codex_directory

_BASE = "agent_backbone.services.runtimes.base"
_HOME = Path.home()
# Every Codex launch opens the sandbox to the network so members reach the API.
_NET = ["-c", "sandbox_workspace_write.network_access=true"]


def _resolve(binary: str):
    return patch(f"{_BASE}.resolve_command", return_value=binary)


class TestHookLaunchArgs:
    def test_claude_gets_backbone_owned_settings(self, tmp_path):
        args = RUNTIMES["claude"].hook_launch_args(tmp_path, tmp_path / "state")
        assert args[0] == "--settings"
        settings_path = tmp_path / "hooks" / "claude-settings.json"
        assert args[1] == str(settings_path)
        saved = json.loads(settings_path.read_text())
        assert "SessionStart" in saved["hooks"]

    def test_runtimes_without_hooks_get_no_args(self, tmp_path):
        assert RUNTIMES["deepcode"].hook_launch_args(tmp_path, tmp_path / "state") == []
        assert RUNTIMES["shell"].hook_launch_args(tmp_path, tmp_path / "state") == []

    def test_missing_dirs_get_no_args(self, tmp_path):
        assert RUNTIMES["claude"].hook_launch_args(None, tmp_path) == []
        assert RUNTIMES["claude"].hook_launch_args(tmp_path, None) == []

    def test_unwritable_data_dir_degrades_to_no_args(self, tmp_path):
        with patch(
            "agent_backbone.hooks.install.install_hook_files",
            side_effect=OSError("read-only"),
        ):
            assert RUNTIMES["claude"].hook_launch_args(tmp_path, tmp_path / "state") == []


class TestBuildCommand:
    def test_claude_command_includes_settings(self, tmp_path):
        with _resolve("/usr/bin/claude"):
            command = RUNTIMES["claude"].build_command(
                model="opus", data_dir=tmp_path, state_dir=tmp_path / "state"
            )
        assert command[:3] == ["/usr/bin/claude", "--model", "opus"]
        assert command[3] == "--settings"
        assert command[4].endswith("claude-settings.json")

    def test_claude_without_dirs_has_no_settings(self):
        with _resolve("/usr/bin/claude"):
            assert RUNTIMES["claude"].build_command() == ["/usr/bin/claude"]

    def test_claude_brief_is_a_system_prompt_file(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent x.")
        with _resolve("/usr/bin/claude"):
            command = RUNTIMES["claude"].build_command(brief_file=brief, resume=True)
        assert command == ["/usr/bin/claude", "--resume", "--append-system-prompt-file", str(brief)]

    def test_shell_stays_none(self, tmp_path):
        assert RUNTIMES["shell"].build_command(data_dir=tmp_path, state_dir=tmp_path) is None

    def test_missing_binary_raises(self):
        with patch(f"{_BASE}.resolve_command", return_value=None):
            try:
                RUNTIMES["codex"].build_command()
            except RuntimeError as exc:
                assert "binary not found" in str(exc)
            else:
                raise AssertionError("expected RuntimeError")

    def test_codex_brief_becomes_initial_prompt(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent x.")
        with _resolve("/bin/codex"):
            command = RUNTIMES["codex"].build_command(model="gpt-5.2", brief_file=brief)
        assert command == ["/bin/codex", *_NET, "--model", "gpt-5.2", "You are agent x."]

    def test_codex_resume_is_a_subcommand(self):
        with _resolve("/bin/codex"):
            assert RUNTIMES["codex"].build_command(resume=True) == [
                "/bin/codex",
                "resume",
                "--last",
                *_NET,
            ]

    def test_codex_sandbox_can_reach_the_backbone_api(self):
        # `backbone tell` from a member must reach 127.0.0.1; the sandbox has
        # no network by default. A resumed session gets it too, after the
        # subcommand like the other `-c` overrides.
        with _resolve("/bin/codex"):
            fresh = RUNTIMES["codex"].build_command(model="gpt-6-astra")
            resumed = RUNTIMES["codex"].build_command(resume="sess-1")
        assert fresh[1:3] == _NET
        assert resumed[1:3] == ["resume", "sess-1"] and resumed[3:5] == _NET

    def test_codex_unreadable_brief_degrades(self, tmp_path):
        with _resolve("/bin/codex"):
            command = RUNTIMES["codex"].build_command(brief_file=tmp_path / "missing.md")
        assert command == ["/bin/codex", *_NET]

    def test_gemini_flags(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent y.")
        with _resolve("/bin/gemini"):
            command = RUNTIMES["gemini"].build_command(
                model="gemini-3-pro", pre_trust=True, brief_file=brief
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
        with _resolve("/bin/gemini"):
            command = RUNTIMES["gemini"].build_command(resume=True, brief_file=brief)
        assert command == ["/bin/gemini", "--resume", "latest"]

    def test_gemini_without_pre_trust_keeps_dialog(self):
        with _resolve("/bin/gemini"):
            assert RUNTIMES["gemini"].build_command() == ["/bin/gemini"]

    def test_opencode_flags(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent z.")
        with _resolve("/bin/opencode"):
            command = RUNTIMES["opencode"].build_command(
                model="opencode/big-pickle", brief_file=brief
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
        with _resolve("/bin/opencode"):
            command = RUNTIMES["opencode"].build_command(resume=True, brief_file=brief)
        assert command == ["/bin/opencode", "--continue"]

    def test_aider_ignores_the_brief_at_launch(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent a.")
        with _resolve("/bin/aider"):
            command = RUNTIMES["aider"].build_command(model="m", brief_file=brief)
        assert command == ["/bin/aider", "--model", "m"]


class TestPreTrust:
    def test_writes_trust_record_preserving_existing_state(self, tmp_path):
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
        config = tmp_path / "claude.json"
        assert pre_trust_directory(tmp_path / "p", claude_config=config) is True
        saved = json.loads(config.read_text())
        assert saved["projects"][str(tmp_path / "p")]["hasTrustDialogAccepted"] is True

    def test_already_trusted_is_a_noop(self, tmp_path):
        config = tmp_path / "claude.json"
        project = tmp_path / "p"
        assert pre_trust_directory(project, claude_config=config) is True
        before = config.read_text()
        assert pre_trust_directory(project, claude_config=config) is True
        assert config.read_text() == before

    def test_corrupt_config_fails_softly(self, tmp_path):
        config = tmp_path / "claude.json"
        config.write_text("{broken")
        assert pre_trust_directory(tmp_path / "p", claude_config=config) is False
        assert config.read_text() == "{broken"


class TestPreTrustPerRuntime:
    def test_each_runtime_answers_its_own_dialog(self, tmp_path):
        with (
            patch("agent_backbone.services.runtimes.claude.pre_trust_directory") as claude,
            patch("agent_backbone.services.runtimes.codex.pre_trust_codex_directory") as codex,
        ):
            RUNTIMES["claude"].pre_trust(tmp_path)
            RUNTIMES["codex"].pre_trust(tmp_path)
            RUNTIMES["gemini"].pre_trust(tmp_path)  # --skip-trust at launch instead
            RUNTIMES["opencode"].pre_trust(tmp_path)  # no trust dialog
        claude.assert_called_once_with(tmp_path)
        codex.assert_called_once_with(tmp_path)


class TestPreTrustCodex:
    def test_scalar_projects_value_fails_softly(self, tmp_path):
        # Valid TOML, unexpected shape: never raise, never rewrite the user's file.
        config = tmp_path / "config.toml"
        config.write_text("projects = 1\n")
        assert pre_trust_codex_directory(tmp_path / "p", codex_config=config) is False
        assert config.read_text() == "projects = 1\n"

    def test_scalar_project_entry_is_left_alone(self, tmp_path):
        project = tmp_path / "p"
        config = tmp_path / "config.toml"
        config.write_text(f'[projects]\n"{project}" = "weird"\n')
        assert pre_trust_codex_directory(project, codex_config=config) is False
        assert "weird" in config.read_text()

    def test_leaves_no_temp_file_behind(self, tmp_path):
        config = tmp_path / "config.toml"
        assert pre_trust_codex_directory(tmp_path / "p", codex_config=config) is True
        assert [p.name for p in tmp_path.iterdir() if p.name != "config.toml"] == []

    def test_appends_trust_record_preserving_config(self, tmp_path):
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
        config = tmp_path / "config.toml"
        assert pre_trust_codex_directory(tmp_path / "p", codex_config=config) is True
        saved = tomllib.loads(config.read_text())
        assert saved["projects"][str(tmp_path / "p")]["trust_level"] == "trusted"

    def test_existing_user_decision_is_left_alone(self, tmp_path):
        project = tmp_path / "p"
        project.mkdir()
        config = tmp_path / "config.toml"
        config.write_text(f'[projects."{project}"]\ntrust_level = "untrusted"\n')
        before = config.read_text()
        assert pre_trust_codex_directory(project, codex_config=config) is False
        assert config.read_text() == before

    def test_corrupt_config_fails_softly(self, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text("model = [broken")
        assert pre_trust_codex_directory(tmp_path / "p", codex_config=config) is False
        assert config.read_text() == "model = [broken"


class TestPreTrustCodexEscaping:
    def test_quotes_and_backslashes_in_the_path_cannot_forge_a_table(self, tmp_path):
        import tomllib

        project = tmp_path / 'x"]\n[projects."victim'
        project.mkdir()
        config = tmp_path / "config.toml"
        assert pre_trust_codex_directory(project, codex_config=config) is True
        saved = tomllib.loads(config.read_text())
        assert saved["projects"] == {str(project.resolve()): {"trust_level": "trusted"}}


class TestDeepCode:
    def test_brief_and_resume(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are agent d.")
        with _resolve("/bin/deepcode"):
            fresh = RUNTIMES["deepcode"].build_command(brief_file=brief)
            resumed = RUNTIMES["deepcode"].build_command(brief_file=brief, resume=True)
        assert fresh == ["/bin/deepcode", "-p", "You are agent d."]
        assert resumed == ["/bin/deepcode", "--last"]

    def test_model_travels_in_the_environment(self):
        # deepcode has no --model flag; MODEL in the environment selects it.
        with _resolve("/bin/deepcode"):
            assert RUNTIMES["deepcode"].build_command(model="deepseek-v4-pro") == ["/bin/deepcode"]
        assert RUNTIMES["deepcode"].launch_env("deepseek-v4-pro") == {"MODEL": "deepseek-v4-pro"}
        assert RUNTIMES["deepcode"].launch_env(None) == {}
        assert RUNTIMES["codex"].launch_env("x") == {}


class TestEffort:
    """``model:effort`` — one spec that every model-naming surface can carry."""

    def test_split_separates_the_effort_from_the_model(self):
        assert split_model_effort("gpt-6-astra:high") == ("gpt-6-astra", "high")
        assert split_model_effort("opus") == ("opus", None)
        assert split_model_effort(None) == (None, None)
        assert split_model_effort("") == (None, None)

    def test_split_normalizes_the_level(self):
        assert split_model_effort("opus: HIGH ") == ("opus", "high")

    def test_codex_effort_is_a_config_override(self):
        with _resolve("/bin/codex"):
            command = RUNTIMES["codex"].build_command(model="gpt-6-astra:high")
        assert command == [
            "/bin/codex",
            "-c",
            "model_reasoning_effort=high",
            *_NET,
            "--model",
            "gpt-6-astra",
        ]

    def test_claude_effort_is_a_flag(self):
        with _resolve("/usr/bin/claude"):
            command = RUNTIMES["claude"].build_command(model="opus:xhigh")
        assert command == ["/usr/bin/claude", "--effort", "xhigh", "--model", "opus"]

    def test_no_effort_leaves_the_command_untouched(self):
        with _resolve("/bin/codex"):
            assert RUNTIMES["codex"].build_command(model="gpt-6-astra") == [
                "/bin/codex",
                *_NET,
                "--model",
                "gpt-6-astra",
            ]

    def test_a_level_the_runtime_does_not_have_is_refused(self):
        # Codex has `ultra`, Claude Code does not: the level is checked against
        # the runtime that will actually be launched.
        with _resolve("/usr/bin/claude"), pytest.raises(RuntimeError, match="no effort 'ultra'"):
            RUNTIMES["claude"].build_command(model="opus:ultra")
        with _resolve("/bin/codex"):
            assert "model_reasoning_effort=ultra" in RUNTIMES["codex"].build_command(
                model="gpt-6-astra:ultra"
            )

    def test_an_effort_without_a_model_is_refused(self):
        # ":high" would otherwise launch the CLI's own default model.
        with _resolve("/bin/codex"), pytest.raises(RuntimeError, match="no model"):
            RUNTIMES["codex"].build_command(model=":high")

    def test_a_runtime_without_an_effort_setting_refuses_rather_than_dropping_it(self):
        with _resolve("/bin/gemini"), pytest.raises(RuntimeError, match="no effort setting"):
            RUNTIMES["gemini"].build_command(model="gemini-3-pro:high")

    def test_effort_survives_resume(self):
        # Codex resumes through a subcommand; `-c` is a global option and still applies.
        with _resolve("/bin/codex"):
            command = RUNTIMES["codex"].build_command(model="gpt-6-astra:max", resume=True)
        assert command[:3] == ["/bin/codex", "-c", "model_reasoning_effort=max"]
        assert "resume" in command


class TestUnattended:
    """``unattended`` adds the CLI's own no-approval switch — or refuses."""

    def test_codex_never_asks_and_keeps_its_sandbox(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are a scout.")
        with _resolve("/bin/codex"):
            command = RUNTIMES["codex"].build_command(
                model="gpt-6-astra:high", brief_file=brief, unattended=True
            )
        assert command == [
            "/bin/codex",
            "-c",
            "model_reasoning_effort=high",
            "-a",
            "never",
            *_NET,
            "--model",
            "gpt-6-astra",
            "You are a scout.",
        ]
        assert not any(arg.startswith("--dangerously-bypass") for arg in command)
        assert RUNTIMES["codex"].sandboxed

    def test_codex_switch_and_writable_dirs_survive_resume(self):
        # Global options, valid before the `resume` subcommand like `-c`.
        with _resolve("/bin/codex"):
            command = RUNTIMES["codex"].build_command(
                resume="sess-1", unattended=True, writable_dirs=("~/.cache/uv",)
            )
        cache = str(_HOME / ".cache/uv")
        assert command[:6] == ["/bin/codex", "-a", "never", "--add-dir", cache, "resume"]

    def test_writable_dirs_open_only_a_sandbox(self):
        # A runtime without a sandbox has nothing to open: everything already is.
        with _resolve("/bin/codex"):
            codex = RUNTIMES["codex"].build_command(writable_dirs=("/a", "/b"))
        assert codex[1:5] == ["--add-dir", "/a", "--add-dir", "/b"]
        with _resolve("/bin/opencode"):
            assert RUNTIMES["opencode"].build_command(writable_dirs=("/a",)) == ["/bin/opencode"]
        assert not RUNTIMES["opencode"].sandboxed

    def test_opencode_gemini_and_claude_have_their_own_switch(self):
        with _resolve("/bin/opencode"):
            assert RUNTIMES["opencode"].build_command(
                model="google/gemini-3.8-flash", unattended=True
            ) == ["/bin/opencode", "--auto", "--model", "google/gemini-3.8-flash"]
        with _resolve("/bin/gemini"):
            assert RUNTIMES["gemini"].build_command(unattended=True) == [
                "/bin/gemini",
                "--approval-mode",
                "yolo",
            ]
        with _resolve("/bin/claude"):
            assert RUNTIMES["claude"].build_command(model="opus", unattended=True) == [
                "/bin/claude",
                "--dangerously-skip-permissions",
                "--model",
                "opus",
            ]

    def test_attended_is_the_default_and_adds_nothing(self):
        with _resolve("/bin/codex"):
            assert "-a" not in RUNTIMES["codex"].build_command(model="gpt-6-astra")

    @pytest.mark.parametrize("runtime", ["deepcode", "aider"])
    def test_a_runtime_without_a_known_switch_is_refused_not_launched_attended(self, runtime):
        with _resolve("/bin/x"), pytest.raises(RuntimeError, match="no unattended switch"):
            RUNTIMES[runtime].build_command(unattended=True)

    def test_a_shell_has_nothing_to_approve(self):
        assert RUNTIMES["shell"].build_command(unattended=True) is None
