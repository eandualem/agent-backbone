"""Tests for workflow registry discovery and lookup."""

from __future__ import annotations

from src.workflow_registry import WorkflowRegistry


async def test_discover_finds_workflows():
    """Registry discovers workflow modules in flows/workflows/."""
    reg = WorkflowRegistry()
    count = reg.discover()
    assert count >= 3  # arclio_start, arclio_stop, full_shutdown


async def test_list_names():
    """list_names returns sorted workflow names."""
    reg = WorkflowRegistry()
    reg.discover()
    names = reg.list_names()
    assert "arclio-start" in names
    assert "arclio-stop" in names
    assert "full-shutdown" in names
    assert names == sorted(names)


async def test_get_known_workflow():
    """get() returns entry for a known workflow."""
    reg = WorkflowRegistry()
    reg.discover()
    entry = reg.get("arclio-start")
    assert entry is not None
    assert entry.name == "arclio-start"
    assert entry.module == "flows.workflows.arclio_start"
    assert entry.description  # has a docstring


async def test_get_unknown_returns_none():
    """get() returns None for unknown workflow names."""
    reg = WorkflowRegistry()
    reg.discover()
    assert reg.get("nonexistent-workflow") is None


async def test_format_list_nonempty():
    """format_list produces human-readable output."""
    reg = WorkflowRegistry()
    reg.discover()
    text = reg.format_list()
    assert "Available workflows:" in text
    assert "arclio-start" in text


async def test_format_list_empty():
    """format_list handles empty registry."""
    reg = WorkflowRegistry()
    text = reg.format_list()
    assert "No workflows registered" in text


async def test_workflows_property():
    """workflows property returns dict copy."""
    reg = WorkflowRegistry()
    reg.discover()
    wf = reg.workflows
    assert isinstance(wf, dict)
    # Modifying the copy shouldn't affect the registry
    wf.clear()
    assert len(reg.workflows) >= 3


async def test_discover_clears_previous():
    """Calling discover() again clears and re-discovers."""
    reg = WorkflowRegistry()
    first = reg.discover()
    second = reg.discover()
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
    """Calling discover() without json_dir only finds Prefect flows (backward compat)."""
    reg = WorkflowRegistry()
    count = reg.discover()
    assert count >= 3  # same as test_discover_finds_workflows
    # Verify all entries are Prefect source
    for entry in reg.workflows.values():
        assert entry.source == "prefect"


async def test_discover_json_empty_dir(tmp_path):
    """Empty JSON dir doesn't add workflows but doesn't fail."""
    reg = WorkflowRegistry()
    count = reg.discover(json_dir=tmp_path)
    # Only Prefect workflows
    assert count >= 3


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
