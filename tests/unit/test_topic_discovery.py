"""Tests for agent_backbone/services/telegram/_topic_discovery.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from agent_backbone.config import BackboneConfig, TelegramConfig
from agent_backbone.services.registry import EntityEntry, EntityRegistry, RepoInfo
from agent_backbone.services.telegram._topic_discovery import (
    TopicDiscovery,
    effective_group_chat_id,
    effective_routes,
    load_discovery,
    process_message_for_discovery,
    resolve_topic_name,
    save_discovery,
)


def _topic_test_registry() -> EntityRegistry:
    """Registry with entities and repos for topic discovery tests."""
    return EntityRegistry(
        entities={
            "ike": EntityEntry(
                session="ike",
                home="~/ws/core/ike",
                groups=[],
                figure="",
                role="",
            ),
            "feynman": EntityEntry(
                session="feynman",
                home="~/orchestration",
                groups=[],
                figure="",
                role="",
            ),
            "leo": EntityEntry(
                session="leo",
                home="~/ws/leo",
                groups=[],
                figure="",
                role="",
            ),
        },
        repos=[
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ],
    )


def _make_config(**kwargs) -> BackboneConfig:
    """Config with standard entities for testing."""
    if "registry" not in kwargs:
        kwargs["registry"] = _topic_test_registry()
    return BackboneConfig(**kwargs)


class TestResolveTopicName:
    def test_named_entity_match(self):
        config = _make_config()
        assert resolve_topic_name("Leo", config) == "leo"

    def test_coding_repo_match(self):
        config = _make_config()
        assert resolve_topic_name("platform-api", config) == "platform-api"

    def test_coding_agents_catchall(self):
        config = _make_config()
        assert resolve_topic_name("Coding Agents", config) == "coding-agents"

    def test_case_insensitive(self):
        config = _make_config()
        assert resolve_topic_name("FEYNMAN", config) == "feynman"
        assert resolve_topic_name("Agent Backbone", config) == "agent-backbone"

    def test_no_match_returns_none(self):
        config = _make_config()
        assert resolve_topic_name("random-topic", config) is None

    def test_whitespace_and_underscore_handling(self):
        config = _make_config()
        assert resolve_topic_name("  Agent  Backbone  ", config) == "agent-backbone"
        assert resolve_topic_name("platform_api", config) == "platform-api"


class TestLoadSaveDiscovery:
    def test_load_existing(self, tmp_path):
        path = tmp_path / "topics.json"
        path.write_text(
            json.dumps(
                {
                    "group_chat_id": -100123,
                    "topic_routes": {"10": "leo", "20": "ike"},
                    "topic_names": {"10": "Leo", "20": "Ike"},
                    "updated_at": 1234567890.0,
                }
            )
        )
        d = load_discovery(path)
        assert d.group_chat_id == -100123
        assert d.topic_routes == {10: "leo", 20: "ike"}
        assert d.topic_names == {10: "Leo", 20: "Ike"}
        assert d.updated_at == 1234567890.0

    def test_load_missing_returns_empty(self, tmp_path):
        d = load_discovery(tmp_path / "nonexistent.json")
        assert d.group_chat_id is None
        assert d.topic_routes == {}
        assert d.topic_names == {}

    def test_load_malformed_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json at all {{{")
        d = load_discovery(path)
        assert d.group_chat_id is None
        assert d.topic_routes == {}

    def test_save_creates_file(self, tmp_path):
        path = tmp_path / "sub" / "topics.json"
        d = TopicDiscovery(group_chat_id=-100, topic_routes={5: "leo"}, updated_at=1.0)
        save_discovery(d, path)
        assert path.exists()
        raw = json.loads(path.read_text())
        assert raw["group_chat_id"] == -100
        assert raw["topic_routes"]["5"] == "leo"

    def test_roundtrip_with_int_key_conversion(self, tmp_path):
        path = tmp_path / "topics.json"
        original = TopicDiscovery(
            group_chat_id=-999,
            topic_routes={42: "feynman", 99: "agent-backbone"},
            topic_names={42: "Feynman", 99: "Agent Backbone"},
            updated_at=5555.0,
        )
        save_discovery(original, path)
        loaded = load_discovery(path)
        assert loaded.group_chat_id == original.group_chat_id
        assert loaded.topic_routes == original.topic_routes
        assert loaded.topic_names == original.topic_names
        assert loaded.updated_at == original.updated_at


class TestEffectiveRoutes:
    def test_config_only(self):
        config = _make_config(telegram=TelegramConfig(topic_routes={10: "leo"}))
        d = TopicDiscovery()
        assert effective_routes(config, d) == {10: "leo"}

    def test_discovery_only(self):
        config = _make_config()
        d = TopicDiscovery(topic_routes={20: "ike"})
        assert effective_routes(config, d) == {20: "ike"}

    def test_config_overrides_discovery_on_conflict(self):
        config = _make_config(telegram=TelegramConfig(topic_routes={10: "from-config"}))
        d = TopicDiscovery(topic_routes={10: "from-discovery"})
        result = effective_routes(config, d)
        assert result[10] == "from-config"

    def test_merges_disjoint(self):
        config = _make_config(telegram=TelegramConfig(topic_routes={10: "leo"}))
        d = TopicDiscovery(topic_routes={20: "ike"})
        result = effective_routes(config, d)
        assert result == {10: "leo", 20: "ike"}


class TestEffectiveGroupChatId:
    def test_config_wins(self):
        config = _make_config(telegram=TelegramConfig(group_chat_id=-100))
        d = TopicDiscovery(group_chat_id=-200)
        assert effective_group_chat_id(config, d) == -100

    def test_discovery_fallback(self):
        config = _make_config()
        d = TopicDiscovery(group_chat_id=-200)
        assert effective_group_chat_id(config, d) == -200

    def test_both_none(self):
        config = _make_config()
        d = TopicDiscovery()
        assert effective_group_chat_id(config, d) is None


def _make_update(
    chat_id: int = -100,
    chat_type: str = "supergroup",
    thread_id: int | None = 10,
    forum_topic_name: str | None = None,
) -> MagicMock:
    """Build a mock Telegram Update for discovery tests."""
    update = MagicMock()
    update.message.chat.type = chat_type
    update.message.chat.id = chat_id
    update.message.message_thread_id = thread_id

    if forum_topic_name:
        update.message.reply_to_message.forum_topic_created.name = forum_topic_name
    else:
        update.message.reply_to_message = None

    return update


class TestProcessMessageForDiscovery:
    def test_discovers_group_chat_id(self, tmp_path):
        path = tmp_path / "topics.json"
        config = _make_config()
        d = TopicDiscovery()
        update = _make_update(chat_id=-999, chat_type="supergroup", thread_id=None)
        update.message.reply_to_message = None

        changed = process_message_for_discovery(update, config, d, path)
        assert changed is True
        assert d.group_chat_id == -999

    def test_discovers_topic_from_forum_topic_created(self, tmp_path):
        path = tmp_path / "topics.json"
        config = _make_config()
        d = TopicDiscovery()
        update = _make_update(thread_id=42, forum_topic_name="Leo")

        changed = process_message_for_discovery(update, config, d, path)
        assert changed is True
        assert d.topic_routes[42] == "leo"
        assert d.topic_names[42] == "Leo"
        # Verify file was saved
        assert path.exists()

    def test_already_known_no_save(self, tmp_path):
        path = tmp_path / "topics.json"
        config = _make_config()
        d = TopicDiscovery(
            group_chat_id=-100,
            topic_routes={42: "leo"},
            topic_names={42: "Leo"},
        )
        update = _make_update(chat_id=-100, thread_id=42, forum_topic_name="Leo")

        changed = process_message_for_discovery(update, config, d, path)
        assert changed is False

    def test_no_forum_topic_created_no_route_added(self, tmp_path):
        path = tmp_path / "topics.json"
        config = _make_config()
        d = TopicDiscovery(group_chat_id=-100)
        update = _make_update(chat_id=-100, thread_id=42)
        # reply_to_message is None (no forum_topic_created)

        changed = process_message_for_discovery(update, config, d, path)
        assert changed is False
        assert 42 not in d.topic_routes
