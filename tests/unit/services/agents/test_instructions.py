"""Preview, ordinary launch and swarm restarts agree on instruction content."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec, LaunchConfig, bootstrap_config, validate_setting
from agent_backbone.services.agents import instruction_preview, start_agent


@pytest.fixture
def setup(tmp_path):
    policies = tmp_path / "policies"
    policies.mkdir()
    for name in ("coding", "python", "tests"):
        (policies / f"{name}.md").write_text(f"Rule for {name}")
    config = replace(
        bootstrap_config(tmp_path),
        launch=LaunchConfig(
            pre_trust=False,
            shared_policy=("coding",),
            tag_policy={"backend": ("python", "coding"), "quality": ("tests",)},
        ),
    )
    spec = AgentSpec(name="api", dir=str(tmp_path), runtime="codex", tags=("quality", "backend"))
    return spec, config


def test_global_then_sorted_tags_deduplicated_across_runtimes(setup):
    spec, config = setup
    assert config.launch.policy_names(spec.tags) == ("coding", "python", "tests")
    results = [
        instruction_preview(replace(spec, runtime=runtime), config)
        for runtime in ("codex", "claude", "gemini", "opencode", "aider")
    ]
    assert len({p["content"] for p in results}) == 1
    assert results[0]["content"].count("Rule for coding") == 1
    assert [s["name"] for s in results[0]["sources"]] == ["base", "coding", "python", "tests"]
    assert results[0]["sources"][1]["scope"] == "global, tag:backend"


def test_custom_base_keeps_required_policies_with_or_without_marker(setup):
    spec, config = setup
    override = config.data_dir / "agent-brief.md"
    override.write_text("Custom {agent_name}")
    preview = instruction_preview(spec, config)
    assert preview["content"].startswith("Custom api")
    assert preview["content"].count("Rule for coding") == 1
    assert all(s["applied"] for s in preview["sources"])
    override.write_text("Custom {agent_name}\n{shared_policy}")
    preview = instruction_preview(spec, config)
    assert "Rule for coding" in preview["content"]
    assert all(s["applied"] for s in preview["sources"])


def test_missing_policy_fails_before_session_creation(setup):
    spec, config = setup
    (config.data_dir / "policies" / "python.md").unlink()
    with pytest.raises(ValueError, match="Cannot read configured shared policy"):
        instruction_preview(spec, config)


@pytest.mark.parametrize("runtime", ["codex", "claude"])
async def test_launch_writes_exact_preview(setup, runtime):
    spec, config = setup
    spec = replace(spec, runtime=runtime)
    expected = instruction_preview(spec, config)["content"]
    with (
        patch(
            "agent_backbone.services.agents.launch.session_exists", AsyncMock(return_value=False)
        ),
        patch("agent_backbone.services.agents.launch.start_session", AsyncMock(return_value=True)),
        patch("agent_backbone.services.runtimes.base.Runtime.build_command", return_value="test"),
        patch("agent_backbone.services.runtimes.base.Runtime.hook_launch_env", return_value={}),
    ):
        result = await start_agent(spec, config, wait=False)
    assert result.ok
    assert (config.data_dir / "briefs" / "api.md").read_text() == expected


def test_swarm_resume_source_and_policies_are_stable(setup):
    spec, config = setup
    spec = replace(spec, tags=("swarm:audit", "role:scout", "backend"))
    role = config.data_dir / "swarms" / "audit" / "api.md"
    role.parent.mkdir(parents=True)
    role.write_text("Inspect the assigned issue.\n")
    first = instruction_preview(spec, config, brief_file=role)
    restart = instruction_preview(spec, config)
    assert first == restart
    assert first["content"].startswith("Inspect the assigned issue.")
    assert "Rule for python" in first["content"]
    assert role.read_text() == "Inspect the assigned issue.\n"


def test_shell_never_receives_instruction_text(setup):
    spec, config = setup
    preview = instruction_preview(replace(spec, runtime="shell"), config)
    assert not preview["enabled"] and preview["content"] == ""


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"": ["coding"]},
        {"bad tag": ["coding"]},
        {"backend": "coding"},
        {"backend": ["../secret"]},
        {"backend": ["coding", "coding"]},
    ],
)
def test_bad_tag_policy_settings_rejected(value):
    with pytest.raises(ValueError):
        validate_setting("agents.tag_policy", value)


async def test_missing_required_policy_blocks_actual_launch(setup):
    spec, config = setup
    (config.data_dir / "policies" / "python.md").unlink()
    with (
        patch(
            "agent_backbone.services.agents.launch.session_exists", AsyncMock(return_value=False)
        ),
        patch("agent_backbone.services.agents.launch.start_session", AsyncMock()) as start,
    ):
        result = await start_agent(spec, config, wait=False)
    assert not result.ok
    assert "Cannot read configured shared policy" in result.evidence[0]
    start.assert_not_called()
