"""Tests for agent_backbone/config.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_backbone.config import (
    BackboneConfig,
    EscalationConfig,
    GatewayConfig,
    HeartbeatConfig,
    JarvisConfig,
)
from agent_backbone.services.registry import EntityEntry, EntityRegistry
from agent_backbone.settings import AppSettings


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    state_dir = home / ".claude" / "state"
    registry_path = state_dir / "entity-registry.json"
    code_base = home / "ws" / "core" / "code" / "WF" / "agent-backbone"
    code_base.mkdir(parents=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "feynman": {
                    "session": "feynman",
                    "home": "~/orchestration",
                    "groups": ["orchestrators"],
                    "figure": "Richard Feynman",
                    "role": "Orchestration Optimizer",
                },
                "ike": {
                    "session": "ike",
                    "home": "~/ws/core/ike",
                    "groups": ["orchestrators"],
                    "figure": "Dwight Eisenhower",
                    "role": "Core Orchestrator",
                },
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))


class TestBackboneConfigDefaults:
    def test_default_constructor(self):
        config = BackboneConfig()
        assert config.github.owner == "eandualem"
        assert config.github.repo == "orchestration"
        assert config.gateway.port == 7120
        assert config.gateway.max_delivery_ids == 100
        assert config.dedup.notification_window_seconds == 10

    def test_nested_gateway(self):
        config = BackboneConfig()
        assert config.gateway.port == 7120
        assert config.gateway.max_delivery_ids == 100

    def test_nested_github(self):
        config = BackboneConfig()
        assert config.github.owner == "eandualem"
        assert config.github.repo == "orchestration"

    def test_nested_entities_non_registry(self):
        config = BackboneConfig()
        assert "elias" in config.entities.skip
        assert config.entities.fallback["coding-agent"] == "ike"

    def test_frozen(self):
        config = BackboneConfig()
        try:
            config.gateway = GatewayConfig(port=1234)  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_nested_frozen(self):
        config = BackboneConfig()
        try:
            config.gateway.port = 1234  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_github_app_ready_when_app_fields_set(self):
        config = BackboneConfig(
            github_app_id=3075015,
            github_app_private_key_path="/tmp/private-key.pem",
            webhook_secret="sec",
        )
        assert config.github_app_ready is True
        assert config.webhook_secret == "sec"

    def test_webhook_secrets_include_github_app_secret(self):
        config = BackboneConfig(
            webhook_secret="legacy-secret",
            github_app_webhook_secret="app-secret",
        )
        assert config.webhook_secrets == ("legacy-secret", "app-secret")

    def test_github_app_fields_can_be_set(self):
        config = BackboneConfig(
            github_app_id=3075015,
            github_app_private_key_path="/tmp/private-key.pem",
        )
        assert config.github_app_id == 3075015
        assert config.github_app_private_key_path == "/tmp/private-key.pem"


class TestFromToml:
    def test_loads_backbone_toml(self):
        """from_toml with the repo's backbone.toml should produce valid config."""
        config = BackboneConfig.from_toml()
        assert config.gateway.port == 7120
        assert config.github.owner == "eandualem"
        assert "feynman" in config.registry.sessions_map
        assert "elias" in config.entities.skip
        assert config.dedup.notification_window_seconds == 10

    def test_nonexistent_file_uses_defaults(self, tmp_path):
        config = BackboneConfig.from_toml(tmp_path / "missing.toml")
        assert config.gateway.port == 7120
        assert config.github.owner == "eandualem"

    def test_custom_toml(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            "[gateway]\nport = 8080\n\n"
            '[github]\nowner = "testorg"\nrepo = "testrepo"\n\n'
            '[entities]\nskip = ["bob"]\n\n'
            '[entities.fallback]\ncoding-agent = "alice"\n\n'
            "[dedup]\nnotification_window_seconds = 30\n"
        )
        config = BackboneConfig.from_toml(toml_file)
        assert config.gateway.port == 8080
        assert config.github.owner == "testorg"
        assert config.github.repo == "testrepo"
        assert "bob" in config.entities.skip
        assert config.entities.fallback["coding-agent"] == "alice"
        assert config.dedup.notification_window_seconds == 30

    def test_partial_toml_fills_defaults(self, tmp_path):
        toml_file = tmp_path / "partial.toml"
        toml_file.write_text("[gateway]\nport = 5555\n")
        config = BackboneConfig.from_toml(toml_file)
        assert config.gateway.port == 5555
        # Everything else should be defaults
        assert config.github.owner == "eandualem"
        assert config.delivery.retention_days == 30

    def test_from_toml_nested_access(self):
        config = BackboneConfig.from_toml()
        assert config.gateway.port == 7120
        assert config.github.owner == "eandualem"
        assert config.github.repo == "orchestration"
        assert config.gateway.max_delivery_ids == 100
        assert config.dedup.notification_window_seconds == 10


class TestEntityRegistry:
    def test_from_toml_loads_registry(self):
        """from_toml loads entity registry when file exists."""
        config = BackboneConfig.from_toml()
        # Registry should be populated from ~/.claude/state/entity-registry.json
        assert "feynman" in config.registry.sessions_map
        assert "ike" in config.registry.sessions_map
        assert config.registry.sessions_map["feynman"] == "feynman"

    def test_from_toml_registry_all_entities(self):
        config = BackboneConfig.from_toml()
        assert "feynman" in config.registry.all_entities
        assert "ike" in config.registry.all_entities

    def test_from_toml_registry_discovers_repos(self):
        config = BackboneConfig.from_toml()
        # Should discover repos from ~/ws/core/code/*/
        assert len(config.registry.repo_names) > 0
        assert "agent-backbone" in config.registry.repo_names

    def test_default_constructor_empty_registry(self):
        config = BackboneConfig()
        assert config.registry.sessions_map == {}
        assert config.registry.all_entities == []
        assert config.registry.repo_names == frozenset()

    def test_constructed_registry(self):
        """Registry constructed with entities exposes sessions_map correctly."""
        registry = EntityRegistry(
            entities={
                "alice": EntityEntry(
                    session="alice-session",
                    home="~/alice",
                    groups=["test"],
                    figure="Alice",
                    role="Tester",
                ),
                "bob": EntityEntry(
                    session="bob-session",
                    home="~/bob",
                    groups=["test"],
                    figure="Bob",
                    role="Builder",
                ),
            },
            repos=[],
        )
        assert registry.sessions_map == {"alice": "alice-session", "bob": "bob-session"}
        assert set(registry.all_entities) == {"alice", "bob"}
        assert registry.repo_names == frozenset()

    def test_missing_registry_file_uses_empty(self, tmp_path):
        """from_toml with missing registry file falls back to empty."""
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            "[registry]\n"
            f'path = "{tmp_path / "nonexistent.json"}"\n'
            f'code_base_dir = "{tmp_path / "nonexistent"}"\n'
        )
        config = BackboneConfig.from_toml(toml_file)
        assert config.registry.sessions_map == {}


class TestPhaseIIConfigs:
    def test_agent_state_defaults(self):
        config = BackboneConfig()
        assert config.agent_state.state_dir == "~/.claude/state"
        assert config.agent_state.stale_threshold_seconds == 300
        assert config.agent_state.state_path == Path.home() / ".claude" / "state"

    def test_database_defaults(self):
        config = BackboneConfig()
        assert config.database.host == "localhost"
        assert config.database.port == 5435
        assert config.database.user == "backbone"
        assert config.database.password == "backbone"
        assert config.database.name == "backbone"
        assert "asyncpg" in config.database.async_url
        assert "5435" in config.database.async_url

    def test_delivery_defaults(self):
        config = BackboneConfig()
        assert config.delivery.retention_days == 30

    def test_scheduling_defaults(self):
        config = BackboneConfig()
        assert config.scheduling.monitor_interval_seconds == 60
        assert config.scheduling.delivery_retry_interval_seconds == 300
        assert config.scheduling.work_pool_name == "agent-pool"


class TestPhaseIIIConfigs:
    def test_telegram_defaults(self):
        config = BackboneConfig()
        assert config.telegram.allowed_chat_ids == []
        assert config.telegram.topic_routes == {}
        assert config.telegram.group_chat_id is None
        assert config.telegram.topic_discovery_file == "~/.claude/state/telegram-topics.json"
        expected = Path.home() / ".claude" / "state" / "telegram-topics.json"
        assert config.telegram.topic_discovery_path == expected

    def test_telegram_topic_routes_from_toml(self, tmp_path):
        toml_file = tmp_path / "routes.toml"
        toml_file.write_text(
            "[telegram]\n"
            "allowed_chat_ids = []\n\n"
            "[telegram.topic_routes]\n"
            '123 = "leo"\n'
            '456 = "coding-agents"\n'
        )
        config = BackboneConfig.from_toml(toml_file)
        assert config.telegram.topic_routes == {123: "leo", 456: "coding-agents"}

    def test_telegram_topic_routes_missing_section_uses_default(self, tmp_path):
        toml_file = tmp_path / "no_routes.toml"
        toml_file.write_text("[telegram]\nallowed_chat_ids = []\n")
        config = BackboneConfig.from_toml(toml_file)
        assert config.telegram.topic_routes == {}

    def test_telegram_group_chat_id_from_toml(self, tmp_path):
        toml_file = tmp_path / "group.toml"
        toml_file.write_text("[telegram]\ngroup_chat_id = -1001234567890\n")
        config = BackboneConfig.from_toml(toml_file)
        assert config.telegram.group_chat_id == -1001234567890

    def test_telegram_topic_discovery_file_default(self, tmp_path):
        toml_file = tmp_path / "no_discovery.toml"
        toml_file.write_text("[telegram]\nallowed_chat_ids = []\n")
        config = BackboneConfig.from_toml(toml_file)
        assert config.telegram.topic_discovery_file == "~/.claude/state/telegram-topics.json"

    def test_telegram_topic_discovery_file_from_toml(self, tmp_path):
        toml_file = tmp_path / "custom_discovery.toml"
        toml_file.write_text('[telegram]\ntopic_discovery_file = "/tmp/my-topics.json"\n')
        config = BackboneConfig.from_toml(toml_file)
        assert config.telegram.topic_discovery_file == "/tmp/my-topics.json"
        assert config.telegram.topic_discovery_path == Path("/tmp/my-topics.json")

    def test_daily_routine_defaults(self):
        config = BackboneConfig()
        assert config.daily_routines.morning_time == "08:00"
        assert config.daily_routines.evening_time == "18:00"
        assert config.daily_routines.timezone == "Africa/Addis_Ababa"


class TestPhaseIVConfigs:
    def test_priority_scoring_defaults(self):
        config = BackboneConfig()
        ps = config.priority_scoring
        assert ps.blocking_weight == 1000.0
        assert ps.dependents_multiplier == 1.5
        assert ps.age_tiebreaker_weight == 0.01
        assert ps.type_weights["spec-gap"] == 100.0
        assert ps.type_weights["bug"] == 90.0
        assert ps.type_weights["task"] == 50.0
        assert ps.type_weights["question"] == 20.0
        assert ps.type_weights["optimization"] == 10.0

    def test_capacity_routing_defaults(self):
        config = BackboneConfig()
        assert config.capacity_routing.busy_threshold_seconds == 1800

    def test_priority_scoring_from_toml(self):
        config = BackboneConfig.from_toml()
        ps = config.priority_scoring
        assert ps.blocking_weight == 1000.0
        assert ps.type_weights["spec-gap"] == 100.0

    def test_priority_scoring_missing_section_uses_defaults(self, tmp_path):
        toml_file = tmp_path / "empty.toml"
        toml_file.write_text("[gateway]\nport = 9877\n")
        config = BackboneConfig.from_toml(toml_file)
        assert config.priority_scoring.blocking_weight == 1000.0
        assert config.priority_scoring.type_weights["task"] == 50.0

    def test_capacity_routing_from_toml(self):
        config = BackboneConfig.from_toml()
        assert config.capacity_routing.busy_threshold_seconds == 1800

    def test_custom_priority_scoring_toml(self, tmp_path):
        toml_file = tmp_path / "custom.toml"
        toml_file.write_text(
            "[priority_scoring]\n"
            "blocking_weight = 500.0\n"
            "dependents_multiplier = 2.0\n"
            "age_tiebreaker_weight = 0.05\n\n"
            "[priority_scoring.type_weights]\n"
            "task = 200.0\n"
            "bug = 150.0\n"
        )
        config = BackboneConfig.from_toml(toml_file)
        assert config.priority_scoring.blocking_weight == 500.0
        assert config.priority_scoring.dependents_multiplier == 2.0
        assert config.priority_scoring.type_weights["task"] == 200.0
        assert config.priority_scoring.type_weights["bug"] == 150.0

    def test_custom_capacity_routing_toml(self, tmp_path):
        toml_file = tmp_path / "custom.toml"
        toml_file.write_text("[capacity_routing]\nbusy_threshold_seconds = 3600\n")
        config = BackboneConfig.from_toml(toml_file)
        assert config.capacity_routing.busy_threshold_seconds == 3600


class TestEscalationConfig:
    def test_defaults(self):
        config = BackboneConfig()
        assert config.escalation.stall_threshold_seconds == 5400
        assert config.escalation.escalation_target == "ike"
        assert config.escalation.escalation_dedup_seconds == 1800

    def test_from_toml(self):
        config = BackboneConfig.from_toml()
        assert config.escalation.stall_threshold_seconds == 5400
        assert config.escalation.escalation_target == "ike"
        assert config.escalation.escalation_dedup_seconds == 1800

    def test_custom_values(self, tmp_path):
        toml_file = tmp_path / "custom.toml"
        toml_file.write_text(
            "[escalation]\n"
            "stall_threshold_seconds = 3600\n"
            'escalation_target = "leo"\n'
            "escalation_dedup_seconds = 900\n"
        )
        config = BackboneConfig.from_toml(toml_file)
        assert config.escalation.stall_threshold_seconds == 3600
        assert config.escalation.escalation_target == "leo"
        assert config.escalation.escalation_dedup_seconds == 900

    def test_missing_section_uses_defaults(self, tmp_path):
        toml_file = tmp_path / "empty.toml"
        toml_file.write_text("[gateway]\nport = 9877\n")
        config = BackboneConfig.from_toml(toml_file)
        assert config.escalation.stall_threshold_seconds == 5400
        assert config.escalation.escalation_target == "ike"

    def test_frozen(self):
        esc = EscalationConfig()
        try:
            esc.stall_threshold_seconds = 999  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestHeartbeatConfig:
    def test_defaults(self):
        config = BackboneConfig()
        assert config.heartbeat.schedule_file == "~/.claude/state/heartbeat-schedules.json"
        assert config.heartbeat.default_timezone == "Africa/Addis_Ababa"
        expected = Path.home() / ".claude" / "state" / "heartbeat-schedules.json"
        assert config.heartbeat.schedule_path == expected

    def test_from_toml(self):
        config = BackboneConfig.from_toml()
        assert config.heartbeat.schedule_file == "~/.claude/state/heartbeat-schedules.json"
        assert config.heartbeat.default_timezone == "Africa/Addis_Ababa"

    def test_custom_values(self, tmp_path):
        toml_file = tmp_path / "custom.toml"
        toml_file.write_text(
            '[heartbeat]\nschedule_file = "/tmp/my-schedules.json"\ndefault_timezone = "UTC"\n'
        )
        config = BackboneConfig.from_toml(toml_file)
        assert config.heartbeat.schedule_file == "/tmp/my-schedules.json"
        assert config.heartbeat.default_timezone == "UTC"
        assert config.heartbeat.schedule_path == Path("/tmp/my-schedules.json")

    def test_missing_section_uses_defaults(self, tmp_path):
        toml_file = tmp_path / "empty.toml"
        toml_file.write_text("[gateway]\nport = 9877\n")
        config = BackboneConfig.from_toml(toml_file)
        assert config.heartbeat.schedule_file == "~/.claude/state/heartbeat-schedules.json"
        assert config.heartbeat.default_timezone == "Africa/Addis_Ababa"

    def test_frozen(self):
        hb = HeartbeatConfig()
        try:
            hb.schedule_file = "/other"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestJarvisConfig:
    def test_defaults_disabled(self):
        """Empty URL means Jarvis is disabled."""
        jc = JarvisConfig()
        assert jc.inject_url == ""
        assert jc.enabled is False

    def test_enabled_with_url(self):
        """Non-empty URL means Jarvis is enabled."""
        jc = JarvisConfig(inject_url="http://localhost:3000/api/assistant/inject")
        assert jc.enabled is True

    def test_from_toml_with_env_var(self, monkeypatch):
        """JARVIS_INJECT_URL env var populates jarvis config."""
        monkeypatch.setenv("JARVIS_INJECT_URL", "http://example.com/inject")
        config = BackboneConfig.from_toml()
        assert config.jarvis.inject_url == "http://example.com/inject"
        assert config.jarvis.enabled is True

    def test_from_toml_sessions_url_env_var(self, monkeypatch):
        """JARVIS_SESSIONS_URL env var populates sessions_url."""
        monkeypatch.setenv("JARVIS_INJECT_URL", "http://example.com/api/assistant/inject")
        monkeypatch.setenv("JARVIS_SESSIONS_URL", "http://example.com/api/sessions")
        config = BackboneConfig.from_toml()
        assert config.jarvis.sessions_url == "http://example.com/api/sessions"

    def test_from_toml_no_env_var(self, monkeypatch):
        """Without JARVIS_INJECT_URL, jarvis is disabled."""
        monkeypatch.delenv("JARVIS_INJECT_URL", raising=False)
        monkeypatch.delenv("JARVIS_SESSIONS_URL", raising=False)
        config = BackboneConfig.from_toml()
        assert config.jarvis.inject_url == ""
        assert config.jarvis.sessions_url == ""
        assert config.jarvis.enabled is False

    def test_backbone_config_default(self):
        """BackboneConfig() has jarvis disabled by default."""
        config = BackboneConfig()
        assert config.jarvis.enabled is False

    def test_frozen(self):
        jc = JarvisConfig()
        try:
            jc.inject_url = "http://x"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestAppSettings:
    def test_build_config_overlays_github_app_webhook_secret(self):
        settings = AppSettings(
            github_app_id=3075015,
            github_app_private_key_path="/tmp/private-key.pem",
            github_app_webhook_secret="app-secret",
        )

        config = settings.build_config()

        assert config.github_app_id == 3075015
        assert config.github_app_private_key_path == "/tmp/private-key.pem"
        assert config.github_app_webhook_secret == "app-secret"
