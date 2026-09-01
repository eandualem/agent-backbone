"""Topic provisioning: one forum topic per registered agent, created by the bot."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from agent_backbone.config import TelegramConfig
from agent_backbone.services.integrations.telegram import TelegramService, _topics
from agent_backbone.services.integrations.telegram._topic_discovery import (
    TopicDiscovery,
    load_discovery,
)


class _FakeTgBot:
    """The slice of python-telegram-bot's Bot the sync uses."""

    def __init__(self, first_thread_id: int = 100):
        self._next = first_thread_id
        self.created: list[str] = []
        self.closed: list[int] = []
        self.reopened: list[int] = []

    async def create_forum_topic(self, chat_id, name):
        self.created.append(name)
        self._next += 1
        return SimpleNamespace(message_thread_id=self._next, name=name)

    async def close_forum_topic(self, chat_id, message_thread_id):
        self.closed.append(message_thread_id)

    async def reopen_forum_topic(self, chat_id, message_thread_id):
        self.reopened.append(message_thread_id)


def _running_bot(
    config, tmp_path, *, discovery=None, **telegram
) -> tuple[TelegramService, _FakeTgBot]:
    telegram.setdefault("allowed_chat_ids", (1,))
    telegram.setdefault("group_chat_id", -100)
    cfg = replace(
        config,
        telegram_token="t",
        telegram=TelegramConfig(**telegram),
        backbone=replace(config.backbone, data_dir=str(tmp_path)),
    )
    bot = TelegramService(cfg)
    fake = _FakeTgBot()
    bot._app = SimpleNamespace(bot=fake)
    bot._running = True
    if discovery is not None:
        bot._discovery = discovery
    _topics._last_failure.clear()
    return bot, fake


class TestSyncTopics:
    async def test_creates_a_topic_per_registered_agent_and_persists(self, config, tmp_path):
        bot, fake = _running_bot(config, tmp_path)
        result = await _topics.sync_topics(bot)
        assert result["created"] == sorted(config.agents.names)
        assert fake.created == sorted(config.agents.names)
        saved = load_discovery(bot.config.telegram_topic_discovery_path)
        assert set(saved.topic_routes.values()) == set(config.agents.names)
        # second pass: nothing to do
        assert (await _topics.sync_topics(bot))["created"] == []
        assert len(fake.created) == len(config.agents.names)

    async def test_existing_routes_are_not_duplicated(self, config, tmp_path):
        names = list(config.agents.names)
        explicit, discovered, *rest = names
        bot, fake = _running_bot(
            config,
            tmp_path,
            topic_routes={7: explicit},
            discovery=TopicDiscovery(topic_routes={9: discovered}),
        )
        result = await _topics.sync_topics(bot)
        assert set(result["created"]) == set(rest)
        assert explicit not in fake.created and discovered not in fake.created

    async def test_forgotten_agent_topic_is_closed_then_reopened_on_return(self, config, tmp_path):
        d = TopicDiscovery(topic_routes={5: "gone", 6: "agents"})
        bot, fake = _running_bot(config, tmp_path, discovery=d)
        result = await _topics.sync_topics(bot)
        assert result["closed"] == ["gone"]
        assert fake.closed == [5] and 5 in d.closed_topics
        assert 6 not in fake.closed  # the catch-all topic is never touched
        # a second pass does not close it again
        assert (await _topics.sync_topics(bot))["closed"] == []

        # the agent comes back under the same name: same topic, reopened
        from tests.conftest import make_agents

        returned = replace(bot.config, agents=make_agents(names=(*config.agents.names, "gone")))
        bot._config_provider = lambda: returned
        result = await _topics.sync_topics(bot)
        assert result["reopened"] == ["gone"] and fake.reopened == [5]
        assert 5 not in d.closed_topics
        assert "gone" not in fake.created

    async def test_explicitly_mapped_topics_are_the_users_and_never_closed(self, config, tmp_path):
        d = TopicDiscovery(topic_routes={5: "gone"})
        bot, fake = _running_bot(config, tmp_path, topic_routes={5: "gone"}, discovery=d)
        await _topics.sync_topics(bot)
        assert fake.closed == []

    async def test_skips_without_a_known_group(self, config, tmp_path):
        bot, fake = _running_bot(config, tmp_path, group_chat_id=None)
        result = await _topics.sync_topics(bot)
        assert "no forum group known" in result["skipped"]
        assert fake.created == []

    async def test_skips_when_disabled_or_not_running(self, config, tmp_path):
        bot, fake = _running_bot(config, tmp_path, auto_topics=False)
        assert "auto_topics" in (await _topics.sync_topics(bot))["skipped"]
        bot, fake = _running_bot(config, tmp_path)
        bot._running = False
        assert "not running" in (await _topics.sync_topics(bot))["skipped"]
        assert fake.created == []

    async def test_telegram_error_is_logged_and_throttled(self, config, tmp_path):
        bot, fake = _running_bot(config, tmp_path)
        fake.create_forum_topic = AsyncMock(side_effect=BadRequest("not enough rights"))
        result = await _topics.sync_topics(bot)
        assert result["skipped"].startswith("telegram error")
        result = await _topics.sync_topics(bot)  # within the retry window: no second attempt
        assert result["skipped"].startswith("recent failure")
        assert fake.create_forum_topic.await_count == 1

    async def test_sync_agents_is_the_integration_hook(self, config, tmp_path):
        bot, fake = _running_bot(config, tmp_path)
        await bot.sync_agents()
        assert fake.created == sorted(config.agents.names)


class TestSyncSerialized:
    async def test_overlapping_syncs_create_each_topic_once(self, config, tmp_path):
        import asyncio

        bot, fake = _running_bot(config, tmp_path)
        barrier = asyncio.Event()
        original = fake.create_forum_topic

        async def slow_create(chat_id, name):
            await barrier.wait()  # hold every creation until both syncs are in flight
            return await original(chat_id, name)

        fake.create_forum_topic = slow_create
        first = asyncio.create_task(bot.sync_agents())
        second = asyncio.create_task(bot.sync_agents())
        await asyncio.sleep(0)
        barrier.set()
        await asyncio.gather(first, second)
        assert sorted(fake.created) == sorted(config.agents.names)  # each exactly once
