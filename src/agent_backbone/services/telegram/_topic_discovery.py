"""Telegram topic auto-discovery — learn thread_id -> session mappings from messages.

The bot discovers topic names from forum_topic_created replies, normalizes
them against the known agents, and persists the mapping to JSON. Explicit
``telegram.topic_routes`` always override discovered routes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

CATCH_ALL_TOPIC = "agents"
"""A topic named like this routes ``agent: message`` lines to any agent."""


@dataclass
class TopicDiscovery:
    """Mutable state for auto-discovered topic mappings."""

    group_chat_id: int | None = None
    topic_routes: dict[int, str] = field(default_factory=dict)
    topic_names: dict[int, str] = field(default_factory=dict)
    updated_at: float = 0.0


def load_discovery(path: Path) -> TopicDiscovery:
    """Load discovery state from JSON file.

    Returns empty TopicDiscovery on missing or malformed file.
    String keys are converted to int (JSON doesn't support int keys).
    """
    try:
        raw = json.loads(path.read_text())
        return TopicDiscovery(
            group_chat_id=raw.get("group_chat_id"),
            topic_routes={int(k): v for k, v in raw.get("topic_routes", {}).items()},
            topic_names={int(k): v for k, v in raw.get("topic_names", {}).items()},
            updated_at=raw.get("updated_at", 0.0),
        )
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return TopicDiscovery()


def save_discovery(discovery: TopicDiscovery, path: Path) -> None:
    """Persist discovery state to JSON with atomic write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "group_chat_id": discovery.group_chat_id,
        "topic_routes": {str(k): v for k, v in discovery.topic_routes.items()},
        "topic_names": {str(k): v for k, v in discovery.topic_names.items()},
        "updated_at": discovery.updated_at,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def resolve_topic_name(name: str, config: BackboneConfig) -> str | None:
    """Normalize a topic name and match against known sessions/repos.

    Normalization: lowercase, spaces and underscores -> hyphens.
    Matches configured agent names, or the catch-all topic name.
    Returns the agent name or None if no match.
    """
    normalized = re.sub(r"[\s_]+", "-", name.strip().lower())

    for agent_name in config.agents.names:
        if normalized == agent_name.lower():
            return agent_name

    if normalized == CATCH_ALL_TOPIC:
        return CATCH_ALL_TOPIC

    return None


def effective_routes(config: BackboneConfig, discovery: TopicDiscovery) -> dict[int, str]:
    """Merge discovered routes with manual config. Config wins on conflict."""
    merged = dict(discovery.topic_routes)
    merged.update(config.telegram.topic_routes)
    return merged


def effective_group_chat_id(config: BackboneConfig, discovery: TopicDiscovery) -> int | None:
    """Return group_chat_id with config taking precedence over discovery."""
    return config.telegram.group_chat_id or discovery.group_chat_id


def process_message_for_discovery(
    update: object,
    config: BackboneConfig,
    discovery: TopicDiscovery,
    path: Path,
) -> bool:
    """Extract discovery data from a Telegram update message.

    Discovers:
    - group_chat_id from supergroup messages
    - topic routes from forum_topic_created in reply_to_message

    Returns True if state was mutated (and saved).
    """
    message = getattr(update, "message", None)
    if message is None:
        return False

    changed = False

    # Discover group_chat_id from supergroup
    chat = getattr(message, "chat", None)
    if chat is not None:
        chat_type = getattr(chat, "type", None)
        chat_id = getattr(chat, "id", None)
        if chat_type == "supergroup" and chat_id is not None and discovery.group_chat_id != chat_id:
            discovery.group_chat_id = chat_id
            changed = True

    # Discover topic mapping from forum_topic_created
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is not None:
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            ftc = getattr(reply, "forum_topic_created", None)
            if ftc is not None:
                topic_name = getattr(ftc, "name", None)
                if topic_name:
                    # Record the raw name for display
                    if discovery.topic_names.get(thread_id) != topic_name:
                        discovery.topic_names[thread_id] = topic_name
                        changed = True

                    # Resolve to session
                    session = resolve_topic_name(topic_name, config)
                    if session and discovery.topic_routes.get(thread_id) != session:
                        discovery.topic_routes[thread_id] = session
                        log.info(
                            "Discovered topic: thread_id=%d name=%r -> session=%s",
                            thread_id,
                            topic_name,
                            session,
                        )
                        changed = True

    if changed:
        discovery.updated_at = time.time()
        save_discovery(discovery, path)

    return changed
