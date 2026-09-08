"""Editor cancellation and conflicting edits must not replace active instructions."""

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone.cli.instructions import edit_file
from agent_backbone.services.swarm import list_brief_templates, render_brief, template_paths


@pytest.mark.parametrize("failure", ["exit", "empty", "concurrent"])
def test_failed_edit_preserves_original(tmp_path, monkeypatch, failure):
    path = tmp_path / "policy.md"
    path.write_text("original")
    monkeypatch.setenv("VISUAL", "example-editor --wait")

    def edit(args):
        assert args[:2] == ["example-editor", "--wait"]
        draft = Path(args[-1])
        assert draft != path and draft.read_text() == "original"
        draft.write_text("" if failure == "empty" else "new policy")
        if failure == "concurrent":
            path.write_text("concurrent policy")
        return 1 if failure == "exit" else 0

    with patch("agent_backbone.cli.instructions.subprocess.call", side_effect=edit):
        with pytest.raises(ValueError):
            edit_file(path, "initial")
    assert path.read_text() == ("concurrent policy" if failure == "concurrent" else "original")


def test_swarm_template_path_matches_new_swarm_rendering(tmp_path):
    source, override = template_paths("scout", tmp_path)
    assert source.is_file() and not override.exists()
    override.parent.mkdir(parents=True)
    override.write_text("Custom scout for {agent_name}")
    assert template_paths("scout", tmp_path)[0] == override
    assert "Custom scout for scout-1" in render_brief(
        "scout", {"agent_name": "scout-1"}, data_dir=tmp_path
    )
    entry = next(t for t in list_brief_templates(tmp_path) if t["name"] == "scout")
    assert entry["source"] == str(override)
    with pytest.raises(ValueError):
        template_paths("../escape", tmp_path)
