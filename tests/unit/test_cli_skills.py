"""`backbone skills`: inspect, add, retag, preview and validate without a running backbone."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone import cli
from agent_backbone.cli import _common
from agent_backbone.config import AgentsConfig, AgentSpec, bootstrap_config


def run(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    return exc.value.code or 0


def _skill(root: Path, name: str, tags: str | None = None) -> Path:
    path = root / name
    path.mkdir(parents=True)
    meta = f"metadata:\n  backbone-tags: {tags}\n" if tags else ""
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d {name}\n{meta}---\n# x\n")
    return path


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("BACKBONE_AGENT", raising=False)
    from dataclasses import replace

    project = tmp_path / "project"
    project.mkdir()
    config = replace(
        bootstrap_config(tmp_path / "data"),
        agents=AgentsConfig(
            specs={
                "leo": AgentSpec(name="leo", dir=str(project), runtime="claude", tags=("coder",))
            }
        ),
    )
    store = config.skills.store_path
    _skill(store, "shared", tags="all")
    _skill(store, "backend", tags="coder")
    with (
        patch.object(_common, "read_config", AsyncMock(return_value=config)),
        patch.object(_common, "api_up", AsyncMock(return_value=False)),
        patch("agent_backbone.cli.skills.commit_store", AsyncMock(return_value=True)),
    ):
        yield config


def test_list_show_and_path(config, capsys):
    assert run(["skills", "list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert {item["name"]: item["reaches"] for item in listing["items"]} == {
        "backend": ["leo"],
        "shared": ["leo"],
    }
    assert run(["skills", "list", "--tag", "coder"]) == 0
    out = capsys.readouterr().out
    assert all(value in out for value in ("Skill", "Purpose", "Agents", "backend", "coder", "leo"))
    assert "shared" not in out
    assert run(["skills", "show", "shared"]) == 0
    assert capsys.readouterr().out.startswith("---\nname: shared\n")
    assert run(["skills", "path", "shared"]) == 0
    assert capsys.readouterr().out.strip() == str(config.skills.store_path / "shared")
    assert run(["skills", "show", "nope"]) == 1
    assert "unknown skill" in capsys.readouterr().err


def test_add_moves_and_tag_rewrites(config, capsys, tmp_path):
    source = _skill(tmp_path / "project" / ".claude" / "skills", "draft")
    assert run(["skills", "add", str(source), "--name", "my-skill", "--tag", "python"]) == 0
    assert "Added my-skill" in capsys.readouterr().out
    assert not source.exists()
    store = config.skills.store_path
    assert (store / "my-skill" / "SKILL.md").read_text().startswith("---\nname: my-skill\n")
    assert run(["skills", "tag", "my-skill", "coder", "python"]) == 0
    assert capsys.readouterr().out.strip() == "my-skill: coder python"
    assert run(["skills", "tag", "my-skill"]) == 0
    assert "reaches nobody" in capsys.readouterr().out
    assert run(["skills", "tag", "nope", "x"]) == 1


def test_add_goes_through_the_api_when_the_backbone_runs(config, capsys, tmp_path):
    source = _skill(tmp_path / "project" / ".agents" / "skills", "draft")
    with (
        patch.object(_common, "api_up", AsyncMock(return_value=True)),
        patch.object(
            _common,
            "api",
            AsyncMock(return_value=(200, {"name": "draft", "tags": ["all"]})),
        ) as api,
    ):
        assert run(["skills", "add", str(source), "--tag", "all"]) == 0
    body = api.await_args.kwargs["json_body"]
    assert body["path"] == str(source.resolve()) and body["tags"] == ["all"]
    assert source.exists()  # the backbone moves it, not this process


def test_preview_and_validate(config, capsys):
    assert run(["skills", "preview", "leo"]) == 0
    out = capsys.readouterr().out
    assert all(
        value in out
        for value in (
            "Skill",
            "Tags",
            "Link state",
            "backend",
            "coder",
            ".claude/skills: will link",
        )
    )
    assert run(["skills", "validate"]) == 0
    assert "Skills valid." in capsys.readouterr().out
    broken = config.skills.store_path / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("nothing")
    assert run(["skills", "validate"]) == 1
    assert "broken: SKILL.md has no frontmatter" in capsys.readouterr().err
    assert run(["skills", "preview", "nobody"]) == 1


def test_parser_shapes():
    parser = cli.build_parser()
    args = parser.parse_args(["skills", "add", "./x", "--tag", "a", "--tag", "b", "--replace"])
    assert args.tag == ["a", "b"] and args.replace
    assert parser.parse_args(["skills", "tag", "n"]).tags == []


def test_bare_agent_name_means_preview(config, capsys):
    from agent_backbone.cli.skills import expand_shorthand

    assert expand_shorthand(["skills", "leo"]) == ["skills", "preview", "leo"]
    assert expand_shorthand(["skills", "leo", "--json"]) == ["skills", "preview", "leo", "--json"]
    assert expand_shorthand(["skills", "list"]) == ["skills", "list"]
    assert expand_shorthand(["-v", "skills", "leo"]) == ["-v", "skills", "preview", "leo"]
    assert expand_shorthand(["skills", "--help"]) == ["skills", "--help"]
    assert expand_shorthand(["status"]) == ["status"]
    assert run(["skills", "leo"]) == 0
    out = capsys.readouterr().out
    assert "backend" in out and ".claude/skills: will link" in out
