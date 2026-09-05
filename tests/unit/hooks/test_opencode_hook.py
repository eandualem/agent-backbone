"""Exercise the shipped plugin with Node; no OpenCode process is required."""

import json
import os
import shutil
import subprocess

import pytest

from agent_backbone.hooks.install import hook_source


def test_state_records_identify_the_runtime_and_session(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const hook = await AgentBackbone();
await hook.event({event: {type: "session.status", properties: {
    sessionID: "opencode-session", status: {type: "busy"}
}}});
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri()],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    state = json.loads((tmp_path / "app.json").read_text())
    assert state["runtime"] == "opencode"
    assert state["session_id"] == "opencode-session"
