"""Installed editing and inspection work without initializing or repairing state."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone import cli
from agent_backbone.cli import _common
from agent_backbone.cli.instructions import edit_file
from agent_backbone.config import AgentsConfig, AgentSpec, LaunchConfig, bootstrap_config
from agent_backbone.templates import append_policies, read_template, render, template_path


def run(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    return exc.value.code or 0


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("BACKBONE_DATABASE_URL", raising=False)
    monkeypatch.delenv("BACKBONE_AGENT", raising=False)


def test_inspection_on_fresh_install_does_not_create_database(tmp_path, capsys):
    with patch("agent_backbone.cli._common.Direct") as direct:
        assert run(["templates", "list", "--json"]) == 0
        listing = json.loads(capsys.readouterr().out)
        assert {"base", "swarm:scout", "swarm:kickoff"} <= {
            row["name"] for row in listing["templates"]
        }
        assert listing["global"] == [] and listing["tags"] == {}
        assert run(["templates", "show", "base"]) == 0
        assert "agent-backbone environment" in capsys.readouterr().out
        assert run(["templates", "validate"]) == 0
        assert run(["templates", "path"]) == 0
    direct.assert_not_called()
    assert not (tmp_path / "data").exists()


def test_init_copies_legacy_and_never_overwrites(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "agent-brief.md").write_text("Legacy {agent_name}")
    assert run(["templates", "init", "base", "swarm:scout"]) == 0
    base = template_path(data, "base")
    assert base.read_text() == "Legacy {agent_name}"
    base.write_text("My own base")
    assert run(["templates", "init"]) == 0
    assert base.read_text() == "My own base"
    assert (data / "agent-brief.md").read_text() == "Legacy {agent_name}"
    assert run(["templates", "show", "base"]) == 0
    assert "My own base" in capsys.readouterr().out
    assert not list(data.glob("*.db"))


def test_empty_override_and_bad_name_fail_clearly(tmp_path, capsys):
    base = template_path(tmp_path / "data", "base")
    base.parent.mkdir(parents=True)
    base.write_text("  ")
    assert run(["templates", "validate"]) == 1
    assert "empty" in capsys.readouterr().err
    assert run(["templates", "show", "../escape"]) == 1
    assert "template name" in capsys.readouterr().err


def test_policy_use_preserves_other_tags_and_normalizes_selector(tmp_path):
    data = tmp_path / "data"
    policy = template_path(data, "policy:python")
    policy.parent.mkdir(parents=True)
    policy.write_text("Required Python practice")
    config = replace(bootstrap_config(), launch=LaunchConfig(tag_policy={"web": ("css",)}))
    with (
        patch.object(_common, "read_config", AsyncMock(return_value=config)),
        patch.object(_common, "api_up", AsyncMock(return_value=True)),
        patch.object(_common, "api", AsyncMock(return_value=(200, {}))) as api,
    ):
        assert run(["templates", "use", "policy:python", "--tag", "python"]) == 0
        assert api.call_args.args[2] == "/api/config/agents.tag_policy"
        assert api.call_args.kwargs["json_body"] == {
            "value": {"web": ["css"], "python": ["python"]}
        }
        assert run(["templates", "use", "--tag", "web"]) == 0
        assert api.call_args.kwargs["json_body"] == {"value": {}}
        api.reset_mock()
        assert run(["templates", "use", "missing"]) == 1
        api.assert_not_called()


async def test_existing_config_inspection_is_read_only(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = Path(bootstrap_config().database_url.removeprefix("sqlite+aiosqlite:///"))
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE settings (key TEXT, value TEXT);
            CREATE TABLE agents (name TEXT, dir TEXT, runtime TEXT, tags TEXT, env TEXT);
            CREATE TABLE agent_watches (agent_name TEXT, repo TEXT);
            INSERT INTO settings VALUES ('agents.tag_policy', '{"python": ["coding"]}');
            INSERT INTO agents VALUES ('api', '/code', 'codex', '["python"]', '{}');
            INSERT INTO agent_watches VALUES ('api', 'acme/app');
        """)
    before = path.read_bytes()
    config = await _common.read_config()
    assert config.agents.get("api").watches == ("acme/app",)
    assert config.launch.policy_names(config.agents.get("api").tags) == ("coding",)
    assert path.read_bytes() == before


async def test_invalid_existing_schema_is_not_repaired(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = Path(bootstrap_config().database_url.removeprefix("sqlite+aiosqlite:///"))
    path.touch()
    with pytest.raises(ValueError, match="Could not read existing configuration"):
        await _common.read_config()
    assert path.read_bytes() == b""


def test_preview_cli_reports_effective_sources(tmp_path, capsys):
    config = replace(
        bootstrap_config(),
        agents=AgentsConfig(
            specs={"api": AgentSpec(name="api", dir=str(tmp_path), runtime="codex")}
        ),
    )
    with patch.object(_common, "read_config", AsyncMock(return_value=config)):
        assert run(["templates", "preview", "api", "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert "**api**" in result["content"]
        assert result["sources"][0]["path"].endswith("templates/base.md")
        assert run(["templates", "preview", "unknown"]) == 1


def test_inherited_source_concurrent_edit_is_preserved(tmp_path, monkeypatch):
    legacy = tmp_path / "agent-brief.md"
    legacy.write_text("Old")
    target = template_path(tmp_path, "base")
    monkeypatch.setenv("VISUAL", "editor")

    def edit(args):
        Path(args[-1]).write_text("My changes")
        legacy.write_text("Someone else's changes")
        return 0

    with patch("agent_backbone.cli.instructions.subprocess.call", side_effect=edit):
        with pytest.raises(ValueError, match="source instructions changed"):
            edit_file(target, "Old", source=legacy)
    assert not target.exists()
    assert legacy.read_text() == "Someone else's changes"


def test_literal_policy_text_and_nonrecursive_facts(tmp_path):
    path = template_path(tmp_path, "policy:rules")
    path.parent.mkdir(parents=True)
    path.write_text("Use {agent_name} and {shared_policy} literally.")
    brief = render("Agent {agent_name}: {repo}", {"agent_name": "{repo}", "repo": "acme/app"})
    assert brief == "Agent {repo}: acme/app"
    content = append_policies(brief, tmp_path, ("rules", "rules"))
    assert content.count("Use {agent_name}") == 1
    with pytest.raises(ValueError, match="at most once"):
        append_policies("{shared_policy}\n{shared_policy}", tmp_path, ("rules",))
    path.write_text("")
    with pytest.raises(ValueError, match="empty"):
        read_template("policy:rules", tmp_path)


def test_validate_checks_assigned_rules_even_without_agents(capsys):
    config = replace(bootstrap_config(), launch=LaunchConfig(tag_policy={"python": ("missing",)}))
    with patch.object(_common, "read_config", AsyncMock(return_value=config)):
        assert run(["templates", "validate"]) == 1
    assert "Cannot read configured shared policy" in capsys.readouterr().err
