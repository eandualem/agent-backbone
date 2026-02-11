"""Tests for src/config.py."""

from __future__ import annotations

from src.config import (
    ALL_ENTITIES,
    CODING_REPOS,
    ENTITY_SESSIONS,
    FALLBACK_ROUTING,
    SKIP_ENTITIES,
    BackboneConfig,
)


class TestEntityMapping:
    def test_all_named_entities_have_sessions(self):
        """Every entity in ALL_ENTITIES must have a session mapping."""
        for entity in ALL_ENTITIES:
            assert entity in ENTITY_SESSIONS

    def test_known_entities(self):
        assert "feynman" in ENTITY_SESSIONS
        assert "ike" in ENTITY_SESSIONS
        assert "leo" in ENTITY_SESSIONS
        assert "ada" in ENTITY_SESSIONS

    def test_elias_is_skipped(self):
        assert "elias" in SKIP_ENTITIES

    def test_coding_agent_fallback(self):
        assert FALLBACK_ROUTING["coding-agent"] == "ike"

    def test_known_coding_repos(self):
        assert "platform-api" in CODING_REPOS
        assert "arclio-assistant" in CODING_REPOS
        assert "mcp-hub" in CODING_REPOS


class TestBackboneConfig:
    def test_defaults(self):
        config = BackboneConfig()
        assert config.github_owner == "eandualem"
        assert config.github_repo == "orchestration"
        assert config.gateway_port == 9877
        assert config.max_delivery_ids == 100
        assert config.notify_dedup_seconds == 5

    def test_custom_values(self):
        config = BackboneConfig(
            github_token="tok",
            gateway_port=8080,
        )
        assert config.github_token == "tok"
        assert config.gateway_port == 8080

    def test_frozen(self):
        config = BackboneConfig()
        try:
            config.gateway_port = 1234  # type: ignore
            assert False, "Should be frozen"
        except AttributeError:
            pass
