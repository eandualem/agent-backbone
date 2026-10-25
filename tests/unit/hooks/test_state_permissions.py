"""Hook output stays private even in a permissive caller environment."""

import os
import stat

import pytest

from agent_backbone.hooks.backbone_state import (
    append_action,
    offer_context,
    offer_steer,
    write_state,
)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_python_hook_outputs_are_private_and_repair_existing_files(tmp_path, runtime):
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    log = root / "actions.jsonl"
    log.write_text("old\n")
    log.chmod(0o666)
    old = os.umask(0)
    try:
        write_state(root, "app", {"runtime": runtime, "launch_id": "launch", "plan_text": "plan"})
        append_action(root, "app", {"action": "comment"})
        offer_context(root, "app", "1", "context")
        offer_steer(root, "app", "launch", "2", "steer", {"session_id": "session"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in root.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
