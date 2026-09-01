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
