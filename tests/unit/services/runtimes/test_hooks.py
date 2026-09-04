"""Hooks are a Runtime capability: what each CLI listens to and how the
backbone wires its hook into a launch without touching the CLI's own files."""

from __future__ import annotations

import json
import tomllib
from unittest.mock import patch

from agent_backbone.hooks import install
from agent_backbone.services.runtimes import RUNTIMES


class TestReportsState:
    def test_hooked_runtimes_say_so(self):
        for rt_id in ("claude", "codex", "gemini", "opencode"):
            assert RUNTIMES[rt_id].reports_state == "hooks + terminal"
        for rt_id in ("deepcode", "aider", "shell"):
            assert RUNTIMES[rt_id].reports_state == "terminal"


class TestClaude:
    def test_launch_settings_file_is_backbone_owned(self, tmp_path):
        args = RUNTIMES["claude"].hook_launch_args(tmp_path, tmp_path / "state")
        assert args[0] == "--settings"
        saved = json.loads((tmp_path / "hooks" / "claude-settings.json").read_text())
        assert set(saved) == {"hooks"}
        assert set(saved["hooks"]) == {e for e, _ in RUNTIMES["claude"].hook_events}
        command = saved["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert f"--state-dir {tmp_path / 'state'}" in command
        assert "claude_hook.py" in command and command.endswith("--tag agent-backbone")
        assert RUNTIMES["claude"].hook_launch_env(tmp_path, tmp_path / "state") == {}

    def test_install_into_a_project(self, tmp_path):
        project = tmp_path / "project"
        path, command = RUNTIMES["claude"].install_hooks(
            tmp_path / "data",
            tmp_path / "data" / "state",
            project_dir=project,
            python="/usr/bin/python3",
        )
        assert path == project / ".claude" / "settings.json"
        assert command.startswith("/usr/bin/python3 ")
        saved = json.loads(path.read_text())
        assert saved["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == command
        # idempotent, and uninstall removes only ours
        RUNTIMES["claude"].install_hooks(
            tmp_path / "data", tmp_path / "data" / "state", project_dir=project
        )
        saved = json.loads(path.read_text())
        assert all(len(entries) == 1 for entries in saved["hooks"].values())
        assert RUNTIMES["claude"].uninstall_hooks(project_dir=project) == path
        assert "hooks" not in json.loads(path.read_text())


class TestCodex:
    def test_launch_overrides_carry_every_event_and_bypass_hook_trust(self, tmp_path):
        args = RUNTIMES["codex"].hook_launch_args(tmp_path, tmp_path / "state")
        assert args[-1] == "--dangerously-bypass-hook-trust"
        overrides = dict(zip(args[0:-1:2], args[1:-1:2]))
        assert set(overrides) == {"-c"} or all(flag == "-c" for flag in args[0:-1:2])
        values = args[1:-1:2]
        events = {v.split("=", 1)[0] for v in values}
        assert events == {f"hooks.{e}" for e, _ in RUNTIMES["codex"].hook_events}
        # every override is valid TOML that Codex can merge
        for value in values:
            key, toml_value = value.split("=", 1)
            doc = tomllib.loads(f"{key} = {toml_value}")
            entry = doc["hooks"][key.split(".", 1)[1]][0]
            assert entry["hooks"][0]["type"] == "command"
            assert "codex_hook.py" in entry["hooks"][0]["command"]
            assert entry["hooks"][0]["timeout"] == 10
        assert (tmp_path / "hooks" / "codex_hook.py").is_file()

    def test_launch_command_carries_the_overrides_before_the_prompt(self, tmp_path):
        brief = tmp_path / "brief.md"
        brief.write_text("You are cx.")
        with patch("agent_backbone.services.runtimes.base.resolve_command", return_value="/c"):
            command = RUNTIMES["codex"].build_command(
                model="gpt-5.6-sol", brief_file=brief, data_dir=tmp_path, state_dir=tmp_path / "s"
            )
            resumed = RUNTIMES["codex"].build_command(
                resume=True, data_dir=tmp_path, state_dir=tmp_path / "s"
            )
        assert command[0] == "/c" and command[1] == "-c"
        assert "--dangerously-bypass-hook-trust" in command
        assert command[-3:] == ["--model", "gpt-5.6-sol", "You are cx."]
        assert resumed[:3] == ["/c", "resume", "--last"] and resumed[3] == "-c"
        assert resumed[-1] == "--dangerously-bypass-hook-trust"

    def test_resume_by_session_id(self):
        with patch("agent_backbone.services.runtimes.base.resolve_command", return_value="/c"):
            assert RUNTIMES["codex"].build_command(resume="01a0")[:3] == ["/c", "resume", "01a0"]
            assert RUNTIMES["claude"].build_command(resume="01a0")[:3] == ["/c", "--resume", "01a0"]
            assert RUNTIMES["gemini"].build_command(resume="01a0")[1:3] == ["--resume", "latest"]

    def test_install_writes_hooks_json_in_codex_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
        path, _ = RUNTIMES["codex"].install_hooks(tmp_path / "data", tmp_path / "state")
        assert path == tmp_path / "home" / ".codex" / "hooks.json"
        assert "PermissionRequest" in json.loads(path.read_text())["hooks"]


class TestGemini:
    def test_launch_env_points_at_a_backbone_owned_system_settings_file(self, tmp_path):
        env = RUNTIMES["gemini"].hook_launch_env(tmp_path, tmp_path / "state")
        path = tmp_path / "hooks" / "gemini-settings.json"
        assert env == {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(path)}
        saved = json.loads(path.read_text())
        assert set(saved["hooks"]) == {e for e, _ in RUNTIMES["gemini"].hook_events}
        hook = saved["hooks"]["AfterAgent"][0]["hooks"][0]
        assert "gemini_hook.py" in hook["command"]
        assert hook["timeout"] == 10_000  # Gemini counts milliseconds
        assert RUNTIMES["gemini"].hook_launch_args(tmp_path, tmp_path / "state") == []

    def test_install_into_a_project(self, tmp_path):
        project = tmp_path / "p"
        path, _ = RUNTIMES["gemini"].install_hooks(
            tmp_path / "d", tmp_path / "s", project_dir=project
        )
        assert path == project / ".gemini" / "settings.json"
        assert "Notification" in json.loads(path.read_text())["hooks"]


class TestOpenCode:
    def test_launch_env_loads_the_plugin_by_file_uri(self, tmp_path):
        env = RUNTIMES["opencode"].hook_launch_env(tmp_path, tmp_path / "state")
        content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        plugin = tmp_path / "hooks" / "opencode_hook.js"
        assert content == {"plugin": [plugin.as_uri()]}
        assert plugin.read_text() == install.hook_source("opencode_hook.js").read_text()

    def test_no_settings_file_to_install_into(self, tmp_path):
        assert RUNTIMES["opencode"].install_hooks(tmp_path, tmp_path / "s") is None
        assert RUNTIMES["opencode"].uninstall_hooks() is None


class TestDegradation:
    def test_unwritable_data_dir_means_no_wiring(self, tmp_path):
        with patch("agent_backbone.hooks.install.install_hook_files", side_effect=OSError("ro")):
            assert RUNTIMES["claude"].hook_launch_args(tmp_path, tmp_path / "s") == []
            assert RUNTIMES["codex"].hook_launch_args(tmp_path, tmp_path / "s") == []
            assert RUNTIMES["gemini"].hook_launch_env(tmp_path, tmp_path / "s") == {}
            assert RUNTIMES["opencode"].hook_launch_env(tmp_path, tmp_path / "s") == {}

    def test_missing_dirs_mean_no_wiring(self, tmp_path):
        for rt_id in ("claude", "codex"):
            assert RUNTIMES[rt_id].hook_launch_args(None, tmp_path) == []
        for rt_id in ("gemini", "opencode"):
            assert RUNTIMES[rt_id].hook_launch_env(tmp_path, None) == {}
