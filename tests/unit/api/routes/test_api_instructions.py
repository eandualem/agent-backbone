"""Read the configured policy sources without launching an agent."""

from dataclasses import replace

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec, LaunchConfig
from agent_backbone.services.runtimes import RUNTIMES


@pytest.fixture
def instructions(api_app):
    config = api_app.state.config
    (config.data_dir / "agent-brief.md").write_text("Hello {agent_name}\n{shared_policy}")
    policies = config.data_dir / "policies"
    policies.mkdir(exist_ok=True)
    (policies / "shared.md").write_text("Shared rule.\n")
    (policies / "coding.md").write_text("Coding rule.\n")
    api_app.state.config = replace(
        config,
        agents=AgentsConfig(
            specs={"worker": AgentSpec(name="worker", dir="/project", tags=("coder",))}
        ),
        launch=LaunchConfig(shared_policy=("shared",), tag_policy={"coder": ("shared", "coding")}),
    )
    return policies


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_instruction_sources_across_runtimes(
    api_client, api_app, auth_headers, instructions, runtime
):
    config = api_app.state.config
    api_app.state.config = replace(
        config,
        agents=AgentsConfig(
            specs={"worker": replace(config.agents.get("worker"), runtime=runtime)}
        ),
    )
    response = await api_client.get("/api/agents/worker/instructions", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "worker" and body["runtime"] == runtime
    assert [s["name"] for s in body["sources"]] == ["base", "shared", "coding"]
    assert [s["text"] for s in body["sources"]] == [
        "Hello {agent_name}\n{shared_policy}",
        "Shared rule.\n",
        "Coding rule.\n",
    ]
    assert body["sources"][1]["scope"] == "global, tag:coder"
    assert body["mode"] == RUNTIMES[runtime].brief_mode
    enabled = body["mode"] != "none"
    assert body["enabled"] == enabled
    assert all(s["applied"] == enabled for s in body["sources"])
    if enabled:
        assert body["content"].startswith("Hello worker\n")
        assert body["content"].count("Shared rule.") == 1
    else:
        assert body["content"] == ""


async def test_disabled_injection_keeps_policy_text(
    api_client, api_app, auth_headers, instructions
):
    config = api_app.state.config
    api_app.state.config = replace(config, launch=replace(config.launch, inject_brief=False))
    (instructions / "coding.md").unlink()
    response = await api_client.get("/api/agents/worker/instructions", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert not body["enabled"] and body["content"] == ""
    assert all(not s["applied"] for s in body["sources"])
    assert body["sources"][1]["text"] == "Shared rule.\n"
    assert body["sources"][2]["text"] is None
    assert "Cannot read instruction source 'coding'." in body["notices"]


async def test_instruction_errors(api_client, auth_headers, instructions):
    assert (await api_client.get("/api/agents/worker/instructions")).status_code == 401
    missing = await api_client.get("/api/agents/missing/instructions", headers=auth_headers)
    assert missing.status_code == 404
    (instructions / "coding.md").unlink()
    broken = await api_client.get("/api/agents/worker/instructions", headers=auth_headers)
    assert broken.status_code == 422
    assert "Cannot read configured shared policy" in broken.json()["detail"]
