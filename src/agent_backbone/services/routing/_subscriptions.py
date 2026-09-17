"""Subscribed source events — who hears about a new item, and at what priority.

Every event is stored in the ``events`` table before routing (the activity
feed and the dedup record: the same mail is never delivered twice), then
grouped per agent and priority so a burst reaches an agent as one message.
Normal batches wait for the agent through the queue; high batches go
through ``safe_deliver`` with ``priority`` and, for a working agent whose
runtime can take hook context, are offered that way (``_delivery``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from agent_backbone.models import SUBSCRIPTION_KIND
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import (
    format_source_event,
    format_subscription_batch,
)

if TYPE_CHECKING:
    from agent_backbone.config import AgentsConfig, BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.sources import SourceEvent

log = logging.getLogger(__name__)

SOURCE = "sources-poll"


def match_subscriptions(agents: AgentsConfig, event: SourceEvent) -> dict[str, bool]:
    """``{agent: high}`` for every agent with a subscription the event matched;
    several matching subscriptions give the highest priority among them."""
    matched: dict[str, bool] = {}
    for spec in agents:
        for sub in spec.subscriptions:
            if sub.source == event.source and sub.filter in event.filters:
                matched[spec.name] = matched.get(spec.name, False) or sub.high
    return matched


async def dispatch_source_events(
    events: Iterable[SourceEvent], config: BackboneConfig, db: BackboneDB
) -> dict[str, int]:
    """Store new events, group them per agent and priority, deliver each group once."""
    summary: dict[str, int] = {}
    batches: dict[tuple[str, bool, str], list[str]] = {}
    for event in events:
        matched = match_subscriptions(config.agents, event)
        if not matched:
            summary["unmatched"] = summary.get("unmatched", 0) + 1
            continue
        event_id = await db.events.record(
            delivery_id=f"{event.source}:{event.id}",
            source=event.source,
            event_type="message",
            sender=event.sender,
            summary=event.subject,
        )
        if event_id is None:
            summary["deduped"] = summary.get("deduped", 0) + 1
            continue
        line = format_source_event(event)
        for agent, high in matched.items():
            batches.setdefault((agent, high, event.source), []).append(line)
        await db.events.mark_processed(event_id, f"subscription: {', '.join(sorted(matched))}")
        summary["events"] = summary.get("events", 0) + 1

    for (agent, high, source), lines in batches.items():
        report = await safe_deliver(
            agent,
            format_subscription_batch(source, lines),
            config,
            db=db,
            source=SOURCE,
            priority=high,
            delivery_kind=SUBSCRIPTION_KIND,
            sender=source,
        )
        key = "delivered" if report.outcome.value == "delivered" else "queued"
        summary[key] = summary.get(key, 0) + 1
        log.info(
            "Subscription batch for %s (%s, %d item(s)): %s%s",
            agent,
            "high" if high else "normal",
            len(lines),
            report.outcome.value,
            f" ({report.queue})" if report.queue else "",
        )
    return summary
