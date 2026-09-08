"""The actual child environment, including inherited denies, survives hook injection."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_backbone.services.runtimes import _opencode_launch


@pytest.mark.parametrize(
    "raw",
    [
        None,
        '{"permission":{"bash":"deny"},"provider":{"custom":{}},"plugin":["other"]}',
        '{/* JSONC */"permission":{"bash":"deny"}}',
    ],
)
def test_child_launcher_preserves_effective_environment(tmp_path, raw):
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
    if raw and "JSONC" in raw:
        assert result.stdout.strip() == raw
        assert raw not in result.stderr
    else:
        content = json.loads(result.stdout)
        assert Path(plugin).as_uri() in content["plugin"]
        if raw:
            assert content["permission"] == {"bash": "deny"}
            assert content["provider"] == {"custom": {}}
            assert "other" in content["plugin"]
