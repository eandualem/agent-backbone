"""The actual child environment, including inherited denies, survives hook injection."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_backbone.services.runtimes import _opencode_launch


@pytest.mark.parametrize(
    "raw,mergeable",
    [
        (None, True),
        ('{"permission":{"bash":"deny"},"provider":{"custom":{}},"plugin":["other"]}', True),
        ('{/* JSONC */"permission":{"bash":"deny"}}', False),
        ('{"permission":{"bash":"deny"},"plugin":"unexpected"}', False),
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
        check=True,
    )
    if not mergeable:
        assert result.stdout.strip() == raw
        assert "Skipping OpenCode hook injection" in result.stderr
        assert raw not in result.stderr
    else:
        content = json.loads(result.stdout)
        assert Path(plugin).as_uri() in content["plugin"]
        if raw:
            assert content["permission"] == {"bash": "deny"}
            assert content["provider"] == {"custom": {}}
            assert "other" in content["plugin"]


def test_child_launcher_keeps_an_existing_hook_once(tmp_path):
    plugin = (tmp_path / "hook with spaces.js").as_uri()
    raw = json.dumps({"plugin": [plugin], "permission": {"bash": "deny"}})
    assert json.loads(_opencode_launch.merge_config(raw, plugin)) == json.loads(raw)
