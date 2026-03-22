"""Tests for workflow registry discovery and lookup."""

from __future__ import annotations

from agent_backbone.services.automation import WorkflowRegistry


async def test_discover_finds_workflows():
    """Registry discovers workflows; with Prefect removed, only JSON are found."""
    reg = WorkflowRegistry()
    count = reg.discover()
    # No @flow decorators remain, so Prefect discovery returns 0
    assert count == 0


async def test_list_names():
    """list_names returns sorted workflow names from JSON discovery."""
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name in ("alpha", "beta"):
            (tmp_path / f"{name}.json").write_text(
                json.dumps({"name": name, "steps": []})
            )
        reg = WorkflowRegistry()
        reg.discover(json_dir=tmp_path)
        names = reg.list_names()

    assert "alpha" in names
    assert "beta" in names
    assert names == sorted(names)


async def test_get_known_workflow(tmp_path):
    """get() returns entry for a known JSON workflow."""
    import json

    wf = {"name": "test-wf", "description": "A test workflow", "steps": []}
    (tmp_path / "test-wf.json").write_text(json.dumps(wf))

    reg = WorkflowRegistry()
    reg.discover(json_dir=tmp_path)
    entry = reg.get("test-wf")
    assert entry is not None
    assert entry.name == "test-wf"
    assert entry.description == "A test workflow"


async def test_get_unknown_returns_none():
    """get() returns None for unknown workflow names."""
    reg = WorkflowRegistry()
    reg.discover()
    assert reg.get("nonexistent-workflow") is None


async def test_format_list_nonempty(tmp_path):
    """format_list produces human-readable output."""
    import json

    (tmp_path / "demo.json").write_text(
        json.dumps({"name": "demo", "description": "Demo workflow", "steps": []})
    )
    reg = WorkflowRegistry()
    reg.discover(json_dir=tmp_path)
    text = reg.format_list()
    assert "Available workflows:" in text
    assert "demo" in text


async def test_format_list_empty():
    """format_list handles empty registry."""
    reg = WorkflowRegistry()
    text = reg.format_list()
    assert "No workflows registered" in text


async def test_workflows_property(tmp_path):
    """workflows property returns dict copy."""
    import json

    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"name": name, "steps": []})
        )
    reg = WorkflowRegistry()
    reg.discover(json_dir=tmp_path)
    wf = reg.workflows
    assert isinstance(wf, dict)
    # Modifying the copy shouldn't affect the registry
    original_count = len(reg.workflows)
    wf.clear()
    assert len(reg.workflows) == original_count


async def test_discover_clears_previous(tmp_path):
    """Calling discover() again clears and re-discovers."""
    import json

    (tmp_path / "wf.json").write_text(
        json.dumps({"name": "wf", "steps": []})
    )
    reg = WorkflowRegistry()
    first = reg.discover(json_dir=tmp_path)
    second = reg.discover(json_dir=tmp_path)
    assert first == second


# ---------------------------------------------------------------------------
# JSON workflow discovery
# ---------------------------------------------------------------------------


async def test_discover_json_workflows(tmp_path):
    """JSON workflows are discovered from a directory."""
    import json

    wf_data = {
        "name": "test-json",
        "description": "A json workflow",
        "steps": [{"action": "start", "session": "x"}],
    }
    (tmp_path / "test-json.json").write_text(json.dumps(wf_data))

    reg = WorkflowRegistry()
    reg.discover(json_dir=tmp_path)
    assert reg.get("test-json") is not None
    entry = reg.get("test-json")
    assert entry.source == "json"
    assert entry.steps == [{"action": "start", "session": "x"}]


async def test_discover_json_with_last_run(tmp_path):
    """JSON workflow last_run is preserved."""
    import json

    wf_data = {
        "name": "timed-wf",
        "description": "",
        "steps": [],
        "last_run": "2026-02-16T00:00:00",
    }
    (tmp_path / "timed-wf.json").write_text(json.dumps(wf_data))

    reg = WorkflowRegistry()
    reg.discover(json_dir=tmp_path)
    entry = reg.get("timed-wf")
    assert entry is not None
    assert entry.last_run == "2026-02-16T00:00:00"


async def test_discover_without_json_dir():
    """Calling discover() without json_dir finds no workflows (no @flow decorators)."""
    reg = WorkflowRegistry()
    count = reg.discover()
    assert count == 0


async def test_discover_json_empty_dir(tmp_path):
    """Empty JSON dir doesn't add workflows but doesn't fail."""
    reg = WorkflowRegistry()
    count = reg.discover(json_dir=tmp_path)
    # No Prefect workflows, no JSON workflows
    assert count == 0


async def test_json_entry_fields(tmp_path):
    """JSON entry has correct field defaults."""
    import json

    (tmp_path / "minimal.json").write_text(json.dumps({"name": "min", "steps": []}))
    reg = WorkflowRegistry()
    reg.discover(json_dir=tmp_path)
    entry = reg.get("min")
    assert entry is not None
    assert entry.flow_fn is None
    assert entry.module == ""
    assert entry.source == "json"
