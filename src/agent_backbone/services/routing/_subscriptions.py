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

from agent_backbone.models import SUBSCRIPTION_KIND, DeliveryOutcome
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
    """Store new events, group them per agent and priority, deliver each group once.

    An event is marked processed only when every matched agent holds it —
    delivered, or stored in the queue. Otherwise it stays replayable: the
    poller keeps its cursor and the next poll hands the event back.
    """
    summary: dict[str, int] = {}
    batches: dict[tuple[str, bool, str], list[str]] = {}
    recipients: dict[int, list[tuple[str, bool, str]]] = {}
    labels: dict[int, str] = {}
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
        if event_id is None or event_id in recipients:
            # Already delivered, or the overlap repeated it inside this poll.
            summary["deduped"] = summary.get("deduped", 0) + 1
            continue
        line = format_source_event(event)
        for agent, high in matched.items():
            key = (agent, high, event.source)
            batches.setdefault(key, []).append(line)
            recipients.setdefault(event_id, []).append(key)
        labels[event_id] = ", ".join(sorted(matched))
        summary["events"] = summary.get("events", 0) + 1

    handed: dict[tuple[str, bool, str], bool] = {}
    for key, lines in batches.items():
        agent, high, source = key
        try:
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
        except Exception:
            log.exception("Subscription batch for %s failed; its events stay replayable", agent)
            handed[key] = False
            summary["failed"] = summary.get("failed", 0) + 1
            continue
        if report.outcome == DeliveryOutcome.DELIVERED:
            outcome = "delivered"
        elif report.queued:
            outcome = "queued"
        else:
            outcome = "failed"
        handed[key] = outcome != "failed"
        summary[outcome] = summary.get(outcome, 0) + 1
        log.info(
            "Subscription batch for %s (%s, %d item(s)): %s%s",
            agent,
            "high" if high else "normal",
            len(lines),
            report.outcome.value,
            f" ({report.queue})" if report.queue else "",
        )
    for event_id, keys in recipients.items():
        if all(handed.get(key) for key in keys):
            await db.events.mark_processed(event_id, f"subscription: {labels[event_id]}")
        else:
            summary["unprocessed"] = summary.get("unprocessed", 0) + 1
    return summary
