"""Tests for agent_backbone/services/telegram/_topic_discovery.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from agent_backbone.config import BackboneConfig, TelegramConfig
from agent_backbone.services.integrations.telegram._topic_discovery import (
    CATCH_ALL_TOPIC,
    TopicDiscovery,
    effective_group_chat_id,
    effective_routes,
    load_discovery,
    process_message_for_discovery,
    resolve_topic_name,
    save_discovery,
)
from tests.conftest import make_agents


def _make_config(**kwargs) -> BackboneConfig:
    kwargs.setdefault(
        "agents", make_agents(names=("ike", "feynman", "leo", "platform-api", "agent-backbone"))
    )
    return BackboneConfig(**kwargs)


class TestResolveTopicName:
    def test_agent_match(self):
        assert resolve_topic_name("Leo", _make_config()) == "leo"

    def test_hyphenated_agent_match(self):
        assert resolve_topic_name("platform-api", _make_config()) == "platform-api"

    def test_catchall(self):
        config = _make_config()
        assert resolve_topic_name("Agents", config) == CATCH_ALL_TOPIC

    def test_case_insensitive(self):
        config = _make_config()
        assert resolve_topic_name("FEYNMAN", config) == "feynman"
        assert resolve_topic_name("Agent Backbone", config) == "agent-backbone"

    def test_no_match_returns_none(self):
        assert resolve_topic_name("random-topic", _make_config()) is None

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

    def test_load_malformed_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json at all {{{")
        d = load_discovery(path)
        assert d.group_chat_id is None

    def test_save_creates_file(self, tmp_path):
        path = tmp_path / "sub" / "topics.json"
        save_discovery(TopicDiscovery(group_chat_id=-100, topic_routes={5: "leo"}), path)
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
        assert loaded == original


class TestEffectiveRoutes:
    def test_config_only(self):
        config = _make_config(telegram=TelegramConfig(topic_routes={10: "leo"}))
        assert effective_routes(config, TopicDiscovery()) == {10: "leo"}

    def test_discovery_only(self):
        assert effective_routes(_make_config(), TopicDiscovery(topic_routes={20: "ike"})) == {
            20: "ike"
        }

    def test_config_overrides_discovery_on_conflict(self):
        config = _make_config(telegram=TelegramConfig(topic_routes={10: "from-config"}))
        result = effective_routes(config, TopicDiscovery(topic_routes={10: "from-discovery"}))
        assert result[10] == "from-config"

    def test_merges_disjoint(self):
        config = _make_config(telegram=TelegramConfig(topic_routes={10: "leo"}))
        assert effective_routes(config, TopicDiscovery(topic_routes={20: "ike"})) == {
            10: "leo",
            20: "ike",
        }


class TestEffectiveGroupChatId:
    def test_config_wins(self):
        config = _make_config(telegram=TelegramConfig(group_chat_id=-100))
        assert effective_group_chat_id(config, TopicDiscovery(group_chat_id=-200)) == -100

    def test_discovery_fallback(self):
        assert effective_group_chat_id(_make_config(), TopicDiscovery(group_chat_id=-200)) == -200

    def test_both_none(self):
        assert effective_group_chat_id(_make_config(), TopicDiscovery()) is None


def _make_update(
    chat_id: int = -100,
    chat_type: str = "supergroup",
    thread_id: int | None = 10,
    forum_topic_name: str | None = None,
) -> MagicMock:
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
        d = TopicDiscovery()
        update = _make_update(chat_id=-999, thread_id=None)
        assert process_message_for_discovery(update, _make_config(), d, tmp_path / "t.json")
        assert d.group_chat_id == -999

    def test_discovers_topic_from_forum_topic_created(self, tmp_path):
        path = tmp_path / "topics.json"
        d = TopicDiscovery()
        update = _make_update(thread_id=42, forum_topic_name="Leo")
        assert process_message_for_discovery(update, _make_config(), d, path)
        assert d.topic_routes[42] == "leo"
        assert d.topic_names[42] == "Leo"
        assert path.exists()

    def test_already_known_no_save(self, tmp_path):
        d = TopicDiscovery(group_chat_id=-100, topic_routes={42: "leo"}, topic_names={42: "Leo"})
        update = _make_update(chat_id=-100, thread_id=42, forum_topic_name="Leo")
        assert not process_message_for_discovery(update, _make_config(), d, tmp_path / "t.json")

    def test_no_forum_topic_created_no_route_added(self, tmp_path):
        d = TopicDiscovery(group_chat_id=-100)
        update = _make_update(chat_id=-100, thread_id=42)
        assert not process_message_for_discovery(update, _make_config(), d, tmp_path / "t.json")
        assert 42 not in d.topic_routes


class TestLoadDiscoveryShapes:
    def test_non_object_shapes_load_as_empty(self, tmp_path):
        # A damaged file must never keep the bot from starting.
        path = tmp_path / "t.json"
        for raw in ("[1, 2]", '"str"', '{"topic_routes": [1]}', '{"topic_names": "x"}'):
            path.write_text(raw)
            assert load_discovery(path) == TopicDiscovery()


class TestClosedTopics:
    def test_roundtrip_and_missing_key_is_empty(self, tmp_path):
        path = tmp_path / "t.json"
        save_discovery(TopicDiscovery(topic_routes={5: "gone"}, closed_topics={5}), path)
        loaded = load_discovery(path)
        assert loaded.closed_topics == {5} and loaded.topic_routes == {5: "gone"}
        path.write_text(json.dumps({"topic_routes": {"5": "gone"}}))  # older file
        assert load_discovery(path).closed_topics == set()
