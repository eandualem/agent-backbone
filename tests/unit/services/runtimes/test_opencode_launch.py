"""The actual child environment, including inherited denies, survives hook injection."""

import json
import os
import subprocess
import sys

import pytest

from agent_backbone.services.runtimes import _opencode_launch


@pytest.mark.parametrize(
    "raw,mergeable",
    [
        (None, True),
        (
            '{"permissions":[{"action":"shell","resource":"*","effect":"deny"}],"provider":{"custom":{}},"plugins":["other"]}',
            True,
        ),
        ('{/* JSONC */"permissions":[{"action":"shell","resource":"*","effect":"deny"}]}', False),
        (
            '{"permissions":[{"action":"shell","resource":"*","effect":"deny"}],"plugins":"unexpected"}',
            False,
        ),
        ('["unexpected"]', False),
        ("invalid config", False),
    ],
)
def test_child_launcher_preserves_effective_environment(tmp_path, raw, mergeable):
    plugin = tmp_path / "hook.js"
    env = dict(os.environ)
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    if raw is not None:
        env["OPENCODE_CONFIG_CONTENT"] = raw
    result = subprocess.run(
        [
            sys.executable,
            _opencode_launch.__file__,
            str(plugin),
            sys.executable,
            "-c",
            'import os; print(os.environ["OPENCODE_CONFIG_CONTENT"])',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if not mergeable:
        assert result.returncode != 0
        assert not result.stdout
        assert "Cannot inject OpenCode hooks" in result.stderr
        assert raw not in result.stderr
    else:
        assert result.returncode == 0
        content = json.loads(result.stdout)
        assert str(plugin.resolve()) in content["plugins"]
        if raw:
            assert content["permissions"] == [
                {"action": "shell", "resource": "*", "effect": "deny"}
            ]
            assert content["provider"] == {"custom": {}}
            assert "other" in content["plugins"]


def test_child_launcher_keeps_an_existing_hook_once(tmp_path):
    plugin = str(tmp_path / "hook with spaces")
    raw = json.dumps({"plugins": [plugin], "permission": {"bash": "deny"}})
    assert json.loads(_opencode_launch.merge_config(raw, plugin)) == json.loads(raw)


def test_v2_config_adds_directory_plugin_without_changing_permission_rules(tmp_path):
    plugin = str(tmp_path / "hooks" / "opencode-v2")
    raw = {
        "permissions": [{"action": "shell", "resource": "*", "effect": "deny"}],
        "plugins": ["existing-plugin"],
    }
    result = json.loads(_opencode_launch.merge_config(json.dumps(raw), plugin))
    assert result == {**raw, "plugins": ["existing-plugin", plugin]}
    assert _opencode_launch.merge_config("{/* JSONC */}", plugin) is None
