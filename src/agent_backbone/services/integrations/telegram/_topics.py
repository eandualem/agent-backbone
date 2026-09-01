"""Topic provisioning — one forum topic per registered agent.

An agent's surface on Telegram is a forum topic named after it. Instead of
asking people to create and name topics, the bot creates them: on start,
whenever the set of agents changes (a config publish) and on the periodic
``integrations-sync`` job. Forgetting an agent closes its topic (history
kept); a re-registered agent gets the same topic reopened. Explicit
``telegram.topic_routes`` and topics discovered from creation messages
count as existing, so nothing is duplicated.

Needs a supergroup with Topics enabled whose id the backbone knows
(``telegram.group_chat_id``, or learned from any message in that group) and
the bot as an administrator with *Manage Topics*. A failing sync is logged
once and retried after ``RETRY_SECONDS``; it never raises.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from telegram.error import TelegramError

from agent_backbone.services.integrations.telegram._topic_discovery import (
    CATCH_ALL_TOPIC,
    agent_topic,
    save_discovery,
)

if TYPE_CHECKING:
    from agent_backbone.services.integrations.telegram.interface import TelegramService

log = logging.getLogger(__name__)

RETRY_SECONDS = 600
_last_failure: dict[int, float] = {}

NO_GROUP_HINT = (
    "no forum group known yet — send any message in the group (or run /identify there), "
    "or set telegram.group_chat_id"
)


async def sync_topics(bot: TelegramService) -> dict:
    """Create, reopen and close topics so they match the registered agents.

    Returns ``{"created": [...], "reopened": [...], "closed": [...],
    "skipped": reason}`` — ``skipped`` is empty when the sync ran.
    """
    result: dict = {"created": [], "reopened": [], "closed": [], "skipped": ""}
    config = bot.config
    if not config.telegram.auto_topics:
        result["skipped"] = "telegram.auto_topics is off"
        return result
    if not bot.running or bot._app is None:
        result["skipped"] = "bot not running"
        return result
    group = bot._effective_group_chat_id()
    if not group:
        result["skipped"] = NO_GROUP_HINT
        log.debug("Topic sync skipped: %s", NO_GROUP_HINT)
        return result
    last = _last_failure.get(group)
    if last is not None and time.monotonic() - last < RETRY_SECONDS:
        result["skipped"] = "recent failure, waiting before retrying"
        return result

    tg = bot._app.bot
    discovery = bot._discovery
    registered = set(config.agents.names)
    changed = False
    try:
        for name in sorted(registered):
            thread_id = agent_topic(config, discovery, name)
            if thread_id is None:
                topic = await tg.create_forum_topic(chat_id=group, name=name)
                discovery.topic_routes[topic.message_thread_id] = name
                discovery.topic_names[topic.message_thread_id] = name
                result["created"].append(name)
                changed = True
            elif thread_id in discovery.closed_topics:
                await tg.reopen_forum_topic(chat_id=group, message_thread_id=thread_id)
                discovery.closed_topics.discard(thread_id)
                result["reopened"].append(name)
                changed = True
        for thread_id, name in list(discovery.topic_routes.items()):
            if name == CATCH_ALL_TOPIC or name in registered:
                continue
            if thread_id in discovery.closed_topics or thread_id in config.telegram.topic_routes:
                continue  # already closed, or an explicit mapping the user owns
            await tg.close_forum_topic(chat_id=group, message_thread_id=thread_id)
            discovery.closed_topics.add(thread_id)
            result["closed"].append(name)
            changed = True
        _last_failure.pop(group, None)
    except TelegramError as exc:
        _last_failure[group] = time.monotonic()
        result["skipped"] = f"telegram error: {exc}"
        log.warning(
            "Telegram topic sync failed (%s); retrying in %ds. Is the bot an administrator "
            "with 'Manage Topics' in a group that has Topics enabled?",
            exc,
            RETRY_SECONDS,
        )
    finally:
        if changed:
            discovery.updated_at = time.time()
            save_discovery(discovery, config.telegram_topic_discovery_path)
    if result["created"] or result["reopened"] or result["closed"]:
        log.info(
            "Telegram topics synced: created=%s reopened=%s closed=%s",
            result["created"],
            result["reopened"],
            result["closed"],
        )
    return result
