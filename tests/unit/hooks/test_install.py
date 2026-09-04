"""Tests for the shared hook files and the settings shape every runtime edits."""

from __future__ import annotations

import json

from agent_backbone.hooks import install

EVENTS = (("SessionStart", None), ("Stop", None), ("PreToolUse", "ExitPlanMode|AskUserQuestion"))


class TestMerge:
    def test_adds_all_events_and_is_idempotent(self):
        cmd = install.hook_command(install.Path("/x/claude_hook.py"), install.Path("/s"))
        once = install.merge_hooks({}, EVENTS, cmd, 10)
        twice = install.merge_hooks(once, EVENTS, cmd, 10)

        assert set(once["hooks"]) == {e for e, _ in EVENTS}
        assert once == twice
        pre = once["hooks"]["PreToolUse"]
        assert pre[0]["matcher"] == "ExitPlanMode|AskUserQuestion"
        assert "matcher" not in once["hooks"]["Stop"][0]
        assert once["hooks"]["Stop"][0]["hooks"][0] == {
            "type": "command",
            "command": cmd,
            "timeout": 10,
        }

    def test_timeout_is_the_runtimes_unit(self):
        merged = install.merge_hooks({}, EVENTS, "h --tag agent-backbone", 10_000)
        assert merged["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 10_000

    def test_preserves_foreign_entries(self):
        foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
        settings = {"permissions": {"allow": ["Bash"]}, "hooks": {"PreToolUse": [foreign]}}
        merged = install.merge_hooks(settings, EVENTS, "python3 h.py --tag agent-backbone", 10)

        assert merged["permissions"] == {"allow": ["Bash"]}
        assert merged["hooks"]["PreToolUse"][0] == foreign
        assert len(merged["hooks"]["PreToolUse"]) == 2

    def test_remove_only_ours(self):
        foreign = {"hooks": [{"type": "command", "command": "echo hi"}]}
        settings = install.merge_hooks(
            {"hooks": {"Stop": [foreign]}}, EVENTS, "h --tag agent-backbone", 10
        )
        cleaned = install.remove_hooks(settings)
        assert cleaned["hooks"] == {"Stop": [foreign]}

    def test_remove_drops_empty_hooks_key(self):
        settings = install.merge_hooks({}, EVENTS, "h --tag agent-backbone", 10)
        assert "hooks" not in install.remove_hooks(settings)


class TestHookFiles:
    def test_every_hook_file_is_copied_and_scripts_are_executable(self, tmp_path):
        hooks_dir = install.install_hook_files(tmp_path)
        assert hooks_dir == tmp_path / "hooks"
        for name in install.HOOK_FILES:
            copied = hooks_dir / name
            assert copied.read_text() == install.hook_source(name).read_text()
            if name.endswith(".py"):
                assert copied.stat().st_mode & 0o111

    def test_hook_command_is_tagged_and_quoted(self, tmp_path):
        command = install.hook_command(
            tmp_path / "my hook.py", tmp_path / "state dir", python="/usr/bin/python3"
        )
        assert command.startswith("/usr/bin/python3 ")
        assert "'" in command  # the spaces are quoted
        assert command.endswith("--tag agent-backbone")

    def test_invalid_settings_json_raises(self, tmp_path):
        bad = tmp_path / "settings.json"
        bad.write_text("{not json")
        try:
            install.load_settings(bad)
        except ValueError as exc:
            assert "not valid JSON" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_settings_roundtrip(self, tmp_path):
        path = tmp_path / "a" / "settings.json"
        install.save_settings(path, {"hooks": {}})
        assert json.loads(path.read_text()) == {"hooks": {}}
