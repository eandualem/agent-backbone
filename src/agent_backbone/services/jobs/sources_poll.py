"""Source polling — the ``sources-poll`` job.

Every ``sources.poll_interval_seconds`` ask each enabled source (Gmail) for
events newer than its cursor that match any subscription filter, and hand
them to ``dispatch_source_events``. The cursor per source is persisted in
``poll_cursors`` (key ``source:<name>``) before the first fetch and after
each batch whose events every recipient holds, with overlap for late
arrivals; the ``events`` table dedups what the overlap repeats. A first run
starts at *now*: a mailbox has years of history and none of it is an event.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from agent_backbone.services.jobs.diagnostics import observe_job
from agent_backbone.services.jobs.github_poll import _iso, _parse
from agent_backbone.services.routing import dispatch_source_events

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.sources import Sources

log = logging.getLogger(__name__)

SOURCE = "sources-poll"
_OVERLAP = timedelta(minutes=2)


def subscription_filters(config: BackboneConfig, source: str) -> list[str]:
    """Every distinct filter any agent subscribed to on ``source``."""
    return sorted(
        {sub.filter for spec in config.agents for sub in spec.subscriptions if sub.source == source}
    )


class SourcesPoller:
    """Scheduler job that pulls subscribed source events and dispatches them."""

    def __init__(
        self,
        config: BackboneConfig | Callable[[], BackboneConfig],
        db: BackboneDB,
        sources: Sources,
    ) -> None:
        self._config_provider = config if callable(config) else (lambda: config)
        self._db = db
        self._sources = sources

    async def run(self) -> dict[str, int]:
        config = self._config_provider()
        summary: dict[str, int] = {}
        for source in self._sources.enabled:
            filters = subscription_filters(config, source.name)
            if not filters:
                continue
            try:
                await self._poll_source(source, filters, config, summary)
            except Exception as exc:
                log.exception("Source poll failed for %s (non-fatal)", source.name)
                await observe_job(
                    self._db, source=SOURCE, stage="source", error_type=type(exc).__name__
                )
            else:
                await observe_job(self._db, source=SOURCE, stage="source")
        if summary:
            log.info("Sources poll: %s", summary)
        return summary

    async def _poll_source(self, source, filters, config, summary) -> None:
        key = f"source:{source.name}"
        poll_started = datetime.now(UTC)
        saved = await self._db.events.poll_cursor(key)
        since = None
        if saved is not None:
            try:
                since = _parse(saved)
                if since > poll_started:
                    raise ValueError("source cursor is in the future")
            except (TypeError, AttributeError, ValueError, OverflowError):
                log.warning("Invalid source cursor for %s; starting now", source.name)
                since = None
        if since is None:
            since = poll_started
            # Persist before the first fetch: a crash mid-batch replays this window.
            await self._db.events.save_poll_cursor(key, _iso(since))
        events = await source.poll(filters, since - _OVERLAP)
        outcome = await dispatch_source_events(events, config, self._db)
        for name, count in outcome.items():
            summary[name] = summary.get(name, 0) + count
        if outcome.get("failed"):
            # Unprocessed events are handed back by the next poll of the same
            # window; the events table drops what already went through.
            log.warning("Source poll for %s kept its cursor: %s", source.name, outcome)
            return
        await self._db.events.save_poll_cursor(key, _iso(poll_started))
