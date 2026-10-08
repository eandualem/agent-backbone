"""The skills store over the API: list, add (a move), retag and preview."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec, SkillsConfig
from agent_backbone.services.runtimes import RUNTIMES

_OPS = "agent_backbone.services.agents.operations"


def _skill(root: Path, name: str, tags: str | None = None) -> Path:
    path = root / name
    path.mkdir(parents=True)
    meta = f"metadata:\n  backbone-tags: {tags}\n" if tags else ""
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d {name}\n{meta}---\n# x\n")
    return path


@pytest.fixture
def store(api_app, tmp_path):
    store = tmp_path / "store"
    project = tmp_path / "project"
    project.mkdir()
    api_app.state.config = replace(
        api_app.state.config,
        skills=SkillsConfig(store=str(store)),
        agents=AgentsConfig(
            specs={
                "leo": AgentSpec(name="leo", dir=str(project), runtime="claude", tags=("coder",)),
                "web": AgentSpec(name="web", dir=str(project), runtime="codex"),
            }
        ),
    )
    _skill(store, "shared", tags="all")
    _skill(store, "backend", tags="coder python")
    with patch("agent_backbone.api.routes.skills.commit_store", AsyncMock(return_value=True)):
        yield store


async def test_list_shows_tags_validity_and_reach(api_client, auth_headers, store):
    broken = _skill(store, "broken")
    (broken / "SKILL.md").write_text("no frontmatter")
    resp = await api_client.get("/api/skills", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["store"] == str(store) and body["git"] is False
    by_name = {item["name"]: item for item in body["items"]}
    assert by_name["shared"]["reaches"] == ["leo", "web"]
    assert by_name["backend"]["reaches"] == ["leo"]
    assert by_name["backend"]["tags"] == ["coder", "python"]
    assert by_name["broken"]["error"] and by_name["broken"]["reaches"] == []


async def test_add_moves_the_directory_and_tags_it(api_client, auth_headers, store):
    outside = _skill(store.parent / "elsewhere" / ".claude" / "skills", "draft")
    refused = await api_client.post(
        "/api/skills", headers=auth_headers, json={"path": str(outside), "tags": ["all"]}
    )
    assert refused.status_code == 422 and "registered agent" in refused.json()["detail"]
    assert outside.exists()
    source = _skill(store.parent / "project" / ".claude" / "skills", "draft")
    resp = await api_client.post(
        "/api/skills",
        headers=auth_headers,
        json={"path": str(source), "name": "my-skill", "tags": ["python"], "actor": "leo"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "my-skill" and resp.json()["tags"] == ["python"]
    assert not source.exists() and (store / "my-skill" / "SKILL.md").is_file()
    again = await api_client.post(
        "/api/skills",
        headers=auth_headers,
        json={"path": str(store.parent / "project" / "nowhere")},
    )
    assert again.status_code == 422


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_a_retag_reaches_running_agents_now(
    api_client, auth_headers, store, api_app, runtime
):
    """leo runs (on ``runtime``); web does not: only leo's links change at once."""
    rt = RUNTIMES[runtime]
    project = store.parent / "project"
    with (
        patch(f"{_OPS}.session_exists", AsyncMock(side_effect=lambda name: name == "leo")),
        patch(f"{_OPS}.resolve_runtime", AsyncMock(return_value=rt)),
    ):
        resp = await api_client.put(
            "/api/skills/backend/tags", headers=auth_headers, json={"tags": ["coder"]}
        )
    assert resp.status_code == 200
    link = project / rt.skill_dirs[0] / "backend"
    assert (link / "SKILL.md").is_file()
    assert any(line.startswith("leo: skills: ") for line in resp.json()["relinked"])
    assert not any(line.startswith("web:") for line in resp.json()["relinked"])
    # Claude Code and Codex load it mid-session; OpenCode is asked to reload.
    marker = api_app.state.config.state_dir / "leo.skills-reload"
    assert marker.exists() == (runtime == "opencode")


async def test_a_runtime_not_verified_to_load_mid_session_says_so(api_client, auth_headers, store):
    with (
        patch(f"{_OPS}.session_exists", AsyncMock(side_effect=lambda name: name == "leo")),
        patch(f"{_OPS}.resolve_runtime", AsyncMock(return_value=RUNTIMES["gemini"])),
    ):
        resp = await api_client.put(
            "/api/skills/backend/tags", headers=auth_headers, json={"tags": ["coder"]}
        )
    assert any("restart it to be sure" in line for line in resp.json()["relinked"])


async def test_retag_and_unknown_skill(api_client, auth_headers, store):
    resp = await api_client.put(
        "/api/skills/backend/tags", headers=auth_headers, json={"tags": ["typescript"]}
    )
    assert resp.status_code == 200 and resp.json()["tags"] == ["typescript"]
    assert 'backbone-tags: "typescript"' in (store / "backend" / "SKILL.md").read_text()
    missing = await api_client.put("/api/skills/nope/tags", headers=auth_headers, json={"tags": []})
    assert missing.status_code == 404


async def test_preview_names_directories_and_link_state(api_client, auth_headers, store):
    resp = await api_client.get("/api/skills/preview/leo", headers=auth_headers)
    assert resp.status_code == 200
    view = resp.json()
    assert view["directories"] == [".claude/skills"]
    assert [s["name"] for s in view["skills"]] == ["backend", "shared"]
    assert view["skills"][0]["links"] == {".claude/skills": "will link"}
    web = (await api_client.get("/api/skills/preview/web", headers=auth_headers)).json()
    assert [s["name"] for s in web["skills"]] == ["shared"]
    assert web["directories"] == [".agents/skills"]
    missing = await api_client.get("/api/skills/preview/nobody", headers=auth_headers)
    assert missing.status_code == 404


async def test_preview_lists_the_repositorys_own_skills_and_how_each_cli_reaches_them(
    api_client, auth_headers, store
):
    project = store.parent / "project"
    _skill(project / ".claude" / "skills", "own")
    _skill(project / ".claude" / "skills", "shared")  # the repository's own wins
    leo = (await api_client.get("/api/skills/preview/leo", headers=auth_headers)).json()
    web = (await api_client.get("/api/skills/preview/web", headers=auth_headers)).json()
    assert [(e["name"], e["state"]) for e in leo["repository"]] == [
        ("own", "read where it is"),
        ("shared", "read where it is"),
    ]
    assert [(e["name"], e["state"]) for e in web["repository"]] == [
        ("own", "will link as .agents/skills/own"),
        ("shared", "will link as .agents/skills/shared"),
    ]
    assert web["repository"][0]["description"] == "d own"
    assert web["skills"][0]["links"] == {
        ".agents/skills": "repository's own .claude/skills/shared wins"
    }
    assert "plugins" in " ".join(web["notices"])


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_preview_lists_the_user_level_skills_the_cli_loads(
    api_client, auth_headers, store, api_app, runtime
):
    """Each CLI's own user-level directories, searched at any depth (tests run on an empty HOME)."""
    leo = api_app.state.config.agents.specs["leo"]
    api_app.state.config = replace(
        api_app.state.config,
        agents=AgentsConfig(specs={"leo": replace(leo, runtime=runtime)}),
    )
    folder = RUNTIMES[runtime].user_skill_dirs({})[0]
    _skill(folder / "synced" / "abc", "mine")
    view = (await api_client.get("/api/skills/preview/leo", headers=auth_headers)).json()
    assert str(folder) in view["user_directories"]
    (entry,) = view["user"]
    assert entry["name"] == "mine" and entry["directory"] == str(folder)
    assert entry["path"] == str(folder / "synced" / "abc" / "mine")


async def test_preview_follows_linked_user_level_skills_once(api_client, auth_headers, store):
    folder = RUNTIMES["claude"].user_skill_dirs({})[0]
    folder.mkdir(parents=True)
    (folder / "linked").symlink_to(_skill(store.parent / "elsewhere", "linked"))
    (folder / "loop").symlink_to(folder)  # a link back up the tree ends there
    view = (await api_client.get("/api/skills/preview/leo", headers=auth_headers)).json()
    assert [entry["name"] for entry in view["user"]] == ["linked"]


async def test_preview_names_the_deep_code_directory(api_client, auth_headers, store, api_app):
    web = api_app.state.config.agents.specs["web"]
    api_app.state.config = replace(
        api_app.state.config,
        agents=AgentsConfig(specs={"docs": replace(web, name="docs", runtime="deepcode")}),
    )
    view = (await api_client.get("/api/skills/preview/docs", headers=auth_headers)).json()
    assert [s["name"] for s in view["skills"]] == ["shared"]
    assert view["directories"] == [".agents/skills"]


async def test_disabled_store_refuses_writes(api_client, auth_headers, api_app):
    from agent_backbone.config import SkillsConfig

    api_app.state.config = replace(api_app.state.config, skills=SkillsConfig(store=""))
    resp = await api_client.post("/api/skills", headers=auth_headers, json={"path": "/x"})
    assert resp.status_code == 400
    assert (await api_client.get("/api/skills", headers=auth_headers)).json()["items"] == []
