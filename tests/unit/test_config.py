"""Tests for agent_backbone/config.py — database-backed settings and agents."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_backbone.config import (
    SETTINGS_DEFAULTS,
    AgentsConfig,
    AgentSpec,
    agents_from_rows,
    bootstrap_config,
    build_config,
    effective_settings,
    validate_setting,
)


class TestDefaults:
    def test_bootstrap_is_generic(self, monkeypatch, tmp_path):
        for var in ("BACKBONE_API_KEY", "GITHUB_TOKEN", "TELEGRAM_TOKEN", "BACKBONE_DATABASE_URL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "d"))
        config = bootstrap_config()
        assert len(config.agents) == 0
        assert config.github_ready is False
        assert config.github_intake == "off"
        assert config.telegram_ready is False
        assert config.escalation.target == ""
        assert config.routing.ignore_targets == frozenset()
        assert config.backbone.port == 7120
        assert config.data_dir == tmp_path / "d"
        assert config.database_url.endswith("d/backbone.db")
        assert config.state_dir == config.data_dir / "state"
        assert config.env_path == config.data_dir / ".env"

    def test_every_default_validates(self):
        for key, default in SETTINGS_DEFAULTS.items():
            assert validate_setting(key, default) == default

    def test_secrets_come_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("BACKBONE_API_KEY", "k")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp")
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s")
        config = bootstrap_config()
        assert config.api_key == "k"
        assert config.github_ready is True
        assert config.github_intake == "webhook"
        assert config.webhook_secrets == ("s",)

    def test_env_file_in_data_dir_is_loaded(self, monkeypatch, tmp_path):
        for var in ("BACKBONE_API_KEY", "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET"):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / ".env").write_text("GITHUB_TOKEN=from-file\n")
        config = bootstrap_config(tmp_path)
        assert config.github_token == "from-file"
        assert config.github_intake == "poll"


class TestValidateSetting:
    def test_unknown_key(self):
        with pytest.raises(KeyError):
            validate_setting("nope.key", 1)

    def test_coerces_types(self):
        assert validate_setting("backbone.port", "8123") == 8123
        assert validate_setting("security.allow_unauthenticated", "true") is True
        assert validate_setting("telegram.allowed_chat_ids", [1, "2"]) == [1, 2]
        assert validate_setting("telegram.topic_routes", {"42": "reviewer"}) == {"42": "reviewer"}

    def test_rejects_bad_values(self):
        with pytest.raises(ValueError):
            validate_setting("backbone.port", "not-a-number")
        with pytest.raises(ValueError):
            validate_setting("github.intake", "carrier-pigeon")


class TestBuildConfig:
    def test_settings_override_defaults(self, tmp_path):
        settings = effective_settings(
            {
                "backbone.port": 7999,
                "routing.ignore_targets": ["human"],
                "telegram.allowed_chat_ids": [1, 2],
                "telegram.topic_routes": {"42": "reviewer"},
                "escalation.target": "reviewer",
                "timing.stale_threshold_seconds": 42,
                "timing.grace_period_seconds": 9,
                "timing.queue_expiry_minutes": 15,
                "github.intake": "poll",
                "security.allow_remote_plan_control": True,
            }
        )
        config = build_config(tmp_path, settings=settings, agents=AgentsConfig())
        assert config.backbone.port == 7999
        assert config.routing.ignore_targets == frozenset({"human"})
        assert config.telegram.allowed_chat_ids == (1, 2)
        assert config.telegram.topic_routes == {42: "reviewer"}
        assert config.escalation.target == "reviewer"
        assert config.agent_state.stale_threshold_seconds == 42
        assert config.delivery.grace_period_seconds == 9
        assert config.delivery.queue_expiry_minutes == 15
        assert config.github.intake == "poll"
        assert config.security.allow_remote_plan_control is True
        assert config.settings["backbone.port"] == 7999

    def test_database_url_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BACKBONE_DATABASE_URL", "sqlite+aiosqlite:///x.db")
        config = build_config(tmp_path, settings=effective_settings({}), agents=AgentsConfig())
        assert config.database_url == "sqlite+aiosqlite:///x.db"

    def test_intake_off_disables_github(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp")
        settings = effective_settings({"github.intake": "off"})
        config = build_config(tmp_path, settings=settings, agents=AgentsConfig())
        config = replace(config, github_token="ghp")
        assert config.github_intake == "off"


class TestAgents:
    def test_agents_from_rows(self):
        rows = [
            {
                "name": "reviewer",
                "dir": "~/code/app",
                "runtime": "codex",
                "model": "gpt-5",
                "repo": "acme/app",
                "tags": ["review"],
                "env": {"FOO": "bar"},
                "description": "",
                "watches": ["acme/coord"],
            },
            {"name": "builder", "dir": "/srv/app", "runtime": "claude", "repo": "acme/app"},
        ]
        agents = agents_from_rows(rows)
        assert agents.names == ["reviewer", "builder"]
        reviewer = agents.get("reviewer")
        assert reviewer == AgentSpec(
            name="reviewer",
            dir="~/code/app",
            runtime="codex",
            model="gpt-5",
            repo="acme/app",
            watches=("acme/coord",),
            tags=("review",),
            env={"FOO": "bar"},
        )
        assert reviewer.repos == ("acme/app", "acme/coord")

    def test_repo_helpers(self):
        agents = agents_from_rows(
            [
                {"name": "a", "dir": "/a", "runtime": "claude", "repo": "acme/app"},
                {"name": "b", "dir": "/b", "runtime": "claude", "repo": "acme/app"},
                {
                    "name": "orch",
                    "dir": "/o",
                    "runtime": "claude",
                    "repo": "acme/orch",
                    "watches": ["acme/app"],
                },
            ]
        )
        assert [s.name for s in agents.owners("ACME/app")] == ["a", "b"]
        assert [s.name for s in agents.watchers("acme/app")] == ["orch"]
        assert [s.name for s in agents.for_repo("acme/app")] == ["a", "b", "orch"]
        assert agents.repos == ["acme/app", "acme/orch"]
        assert "orch" in agents
        assert agents.dir_for("orch") == "/o"
