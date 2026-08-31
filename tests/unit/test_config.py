"""Tests for agent_backbone/config.py."""

from __future__ import annotations

import pytest

from agent_backbone.config import (
    AgentSpec,
    BackboneConfig,
    find_config_file,
)

_TOML = """
[backbone]
data_dir = "{data_dir}"
port = 7999

[agents.reviewer]
dir = "~/code/app"
runtime = "codex"
model = "gpt-5"
repo = "acme/app"
tags = ["review"]
env = {{ FOO = "bar" }}

[agents.builder]
dir = "/srv/app"

[github]
repo = "acme/coord"
mode = "poll"

[routing]
ignore_targets = ["human"]

[telegram]
allowed_chat_ids = [1, 2]
[telegram.topic_routes]
42 = "reviewer"

[escalation]
target = "reviewer"

[security]
allow_remote_plan_control = true
"""


@pytest.fixture
def toml_file(tmp_path):
    path = tmp_path / "backbone.toml"
    path.write_text(_TOML.format(data_dir=str(tmp_path / "data")))
    return path


class TestDefaults:
    def test_defaults_are_generic(self, monkeypatch):
        for var in ("BACKBONE_API_KEY", "GITHUB_TOKEN", "TELEGRAM_TOKEN", "BACKBONE_DATA_DIR"):
            monkeypatch.delenv(var, raising=False)
        config = BackboneConfig.from_dict({})
        assert len(config.agents) == 0
        assert config.github.enabled is False
        assert config.github_ready is False
        assert config.telegram_ready is False
        assert config.escalation.target == ""
        assert config.routing.ignore_targets == frozenset()
        assert config.backbone.port == 7120
        assert config.database_url.endswith("agent-backbone/backbone.db")
        assert config.state_dir == config.data_dir / "state"


class TestLoad:
    def test_loads_sections(self, toml_file, monkeypatch, tmp_path):
        monkeypatch.setenv("BACKBONE_API_KEY", "k")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp")
        config = BackboneConfig.load(toml_file)

        assert config.source_path == toml_file
        assert config.backbone.port == 7999
        assert config.data_dir == tmp_path / "data"
        assert config.agents.names == ["reviewer", "builder"]
        reviewer = config.agents.get("reviewer")
        assert reviewer == AgentSpec(
            name="reviewer",
            dir="~/code/app",
            runtime="codex",
            model="gpt-5",
            repo="acme/app",
            tags=("review",),
            env={"FOO": "bar"},
        )
        assert config.agents.get("builder").runtime == "claude"
        assert config.agents.for_repo("ACME/app")[0].name == "reviewer"
        assert config.github.repo == "acme/coord"
        assert config.github.owner == "acme" and config.github.name == "coord"
        assert config.github.mode == "poll"
        assert config.github_ready is True
        assert config.routing.ignore_targets == frozenset({"human"})
        assert config.telegram.allowed_chat_ids == (1, 2)
        assert config.telegram.topic_routes == {42: "reviewer"}
        assert config.escalation.target == "reviewer"
        assert config.security.allow_remote_plan_control is True
        assert config.api_key == "k"

    def test_env_overrides(self, toml_file, monkeypatch, tmp_path):
        monkeypatch.setenv("BACKBONE_PORT", "8123")
        monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "other"))
        monkeypatch.setenv("BACKBONE_DATABASE_URL", "sqlite+aiosqlite:///x.db")
        monkeypatch.setenv("BACKBONE_ALLOW_UNAUTHENTICATED", "1")
        config = BackboneConfig.load(toml_file)
        assert config.backbone.port == 8123
        assert config.data_dir == tmp_path / "other"
        assert config.database_url == "sqlite+aiosqlite:///x.db"
        assert config.security.allow_unauthenticated is True

    def test_missing_file_uses_defaults(self, tmp_path):
        config = BackboneConfig.load(tmp_path / "nope.toml")
        assert len(config.agents) == 0

    def test_agent_requires_dir(self):
        with pytest.raises(ValueError, match="missing required key 'dir'"):
            BackboneConfig.from_dict({"agents": {"x": {"runtime": "claude"}}})

    def test_github_app_readiness(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_APP_ID", "12")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "/k.pem")
        config = BackboneConfig.from_dict({"github": {"repo": "a/b"}})
        assert config.github_app_ready is True
        assert config.github_ready is True


class TestFindConfigFile:
    def test_env_var_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKBONE_CONFIG", str(tmp_path / "custom.toml"))
        assert find_config_file() == tmp_path / "custom.toml"

    def test_walks_up_from_start(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BACKBONE_CONFIG", raising=False)
        (tmp_path / "backbone.toml").write_text("")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_config_file(nested) == tmp_path / "backbone.toml"

    def test_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BACKBONE_CONFIG", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
        assert find_config_file(tmp_path) is None
