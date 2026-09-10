"""The skills store over the API: list, add (a move), retag and preview."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec, SkillsConfig


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


async def test_disabled_store_refuses_writes(api_client, auth_headers, api_app):
    from agent_backbone.config import SkillsConfig

    api_app.state.config = replace(api_app.state.config, skills=SkillsConfig(store=""))
    resp = await api_client.post("/api/skills", headers=auth_headers, json={"path": "/x"})
    assert resp.status_code == 400
    assert (await api_client.get("/api/skills", headers=auth_headers)).json()["items"] == []
