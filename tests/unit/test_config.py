"""Tests for agent_backbone/config.py — database-backed settings and agents."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from agent_backbone.config import (
    SECRET_ENV_KEYS,
    SETTINGS_DEFAULTS,
    AgentsConfig,
    AgentSpec,
    agents_from_rows,
    bootstrap_config,
    build_config,
    effective_settings,
    load_secrets,
    session_secret_keys,
    validate_setting,
)


class TestDefaults:
    def test_bootstrap_is_generic(self, monkeypatch, tmp_path):
        for var in (
            "BACKBONE_API_KEY",
            "GITHUB_TOKEN",
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY_PATH",
            "GITHUB_WEBHOOK_SECRET",
            "TELEGRAM_TOKEN",
            "BACKBONE_DATABASE_URL",
        ):
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
        assert config.webhook_secret == "s"

    def test_env_file_in_data_dir_is_loaded(self, monkeypatch, tmp_path):
        for var in ("BACKBONE_API_KEY", "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET"):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / ".env").write_text("GITHUB_TOKEN=from-file\n")
        config = bootstrap_config(tmp_path)
        assert config.github_token == "from-file"
        assert config.github_intake == "poll"


class TestSecretsStayOutOfTheEnvironment:
    """Issue #81: the daemon spawns the tmux server, so anything in its own
    environment reaches every agent session started on that server."""

    def test_env_file_never_reaches_os_environ(self, monkeypatch, tmp_path):
        for var in ("GITHUB_TOKEN", "TELEGRAM_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / ".env").write_text("GITHUB_TOKEN=from-file\nTELEGRAM_TOKEN=tg\n")

        config = bootstrap_config(tmp_path)

        assert config.github_token == "from-file"
        assert config.telegram_token == "tg"
        assert "GITHUB_TOKEN" not in os.environ
        assert "TELEGRAM_TOKEN" not in os.environ

    def test_load_secrets_returns_the_merged_mapping(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        (tmp_path / ".env").write_text("GITHUB_TOKEN=from-file\n")
        assert load_secrets(tmp_path)["GITHUB_TOKEN"] == "from-file"

    def test_process_environment_wins_over_the_file(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("GITHUB_TOKEN=from-file\n")
        monkeypatch.setenv("GITHUB_TOKEN", "from-shell")
        assert bootstrap_config(tmp_path).github_token == "from-shell"

    def test_missing_env_file_is_fine(self, tmp_path):
        assert load_secrets(tmp_path).get("GITHUB_TOKEN", "") == os.environ.get("GITHUB_TOKEN", "")


class TestSessionSecretKeys:
    def test_covers_the_known_backbone_secrets(self, tmp_path):
        keys = session_secret_keys(tmp_path)
        for known in SECRET_ENV_KEYS:
            assert known in keys

    def test_covers_whatever_the_user_put_in_env(self, tmp_path):
        (tmp_path / ".env").write_text("MY_PRIVATE_KEY=x\n# COMMENTED=y\n")
        keys = session_secret_keys(tmp_path)
        assert "MY_PRIVATE_KEY" in keys
        assert "COMMENTED" not in keys

    def test_no_duplicates(self, tmp_path):
        (tmp_path / ".env").write_text("GITHUB_TOKEN=x\n")
        keys = session_secret_keys(tmp_path)
        assert len(keys) == len(set(keys))

    def test_without_a_data_dir_falls_back_to_the_known_names(self):
        assert session_secret_keys(None) == SECRET_ENV_KEYS

    def test_database_url_is_a_secret(self, tmp_path):
        # A PostgreSQL URL carries the password; it arrives via the process
        # environment (not .env), so only the known-names list catches it.
        assert "BACKBONE_DATABASE_URL" in session_secret_keys(tmp_path)


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
        assert config.timing.stale_threshold_seconds == 42
        assert config.timing.grace_period_seconds == 9
        assert config.timing.queue_expiry_minutes == 15
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
        assert agents.repos == ["acme/app", "acme/orch"]
        assert "orch" in agents
