"""Tests for hook installation into Claude Code settings."""

from __future__ import annotations

import json

from agent_backbone.hooks import install


class TestMerge:
    def test_adds_all_events_and_is_idempotent(self):
        cmd = install.hook_command(install.Path("/x/claude_hook.py"), install.Path("/s"))
        once = install.merge_claude_hooks({}, cmd)
        twice = install.merge_claude_hooks(once, cmd)

        assert set(once["hooks"]) == {e for e, _ in install.CLAUDE_EVENTS}
        assert once == twice
        pre = once["hooks"]["PreToolUse"]
        assert pre[0]["matcher"] == "ExitPlanMode|AskUserQuestion"
        assert "matcher" not in once["hooks"]["Stop"][0]
        assert once["hooks"]["Stop"][0]["hooks"][0]["command"] == cmd

    def test_preserves_foreign_entries(self):
        foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
        settings = {"permissions": {"allow": ["Bash"]}, "hooks": {"PreToolUse": [foreign]}}
        merged = install.merge_claude_hooks(settings, "python3 h.py --tag agent-backbone")

        assert merged["permissions"] == {"allow": ["Bash"]}
        assert merged["hooks"]["PreToolUse"][0] == foreign
        assert len(merged["hooks"]["PreToolUse"]) == 2

    def test_remove_only_ours(self):
        foreign = {"hooks": [{"type": "command", "command": "echo hi"}]}
        settings = install.merge_claude_hooks(
            {"hooks": {"Stop": [foreign]}}, "h --tag agent-backbone"
        )
        cleaned = install.remove_claude_hooks(settings)
        assert cleaned["hooks"] == {"Stop": [foreign]}

    def test_remove_drops_empty_hooks_key(self):
        settings = install.merge_claude_hooks({}, "h --tag agent-backbone")
        assert "hooks" not in install.remove_claude_hooks(settings)


class TestInstallClaude:
    def test_installs_script_and_settings(self, tmp_path):
        data_dir = tmp_path / "data"
        project = tmp_path / "project"
        settings_path, command = install.install_claude(
            data_dir, data_dir / "state", project_dir=project, python="/usr/bin/python3"
        )

        script = data_dir / "hooks" / "claude_hook.py"
        assert script.is_file()
        assert script.read_text() == install.hook_script_source().read_text()
        assert settings_path == project / ".claude" / "settings.json"
        assert command.startswith('/usr/bin/python3 "')
        assert f'--state-dir "{data_dir / "state"}"' in command
        assert command.endswith("--tag agent-backbone")
        saved = json.loads(settings_path.read_text())
        assert saved["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == command

    def test_reinstall_keeps_one_entry_per_event(self, tmp_path):
        project = tmp_path / "p"
        install.install_claude(tmp_path / "d", tmp_path / "s", project_dir=project)
        install.install_claude(tmp_path / "d", tmp_path / "s", project_dir=project)
        saved = json.loads((project / ".claude" / "settings.json").read_text())
        assert all(len(entries) == 1 for entries in saved["hooks"].values())

    def test_uninstall(self, tmp_path):
        project = tmp_path / "p"
        install.install_claude(tmp_path / "d", tmp_path / "s", project_dir=project)
        path = install.uninstall_claude(project_dir=project)
        assert "hooks" not in json.loads(path.read_text())

    def test_invalid_settings_json_raises(self, tmp_path):
        project = tmp_path / "p"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text("{not json")
        try:
            install.install_claude(tmp_path / "d", tmp_path / "s", project_dir=project)
        except ValueError as exc:
            assert "not valid JSON" in str(exc)
        else:
            raise AssertionError("expected ValueError")
