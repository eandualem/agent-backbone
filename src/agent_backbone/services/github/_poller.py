"""GitHub polling connector.

Used two ways:

* **poll intake** (no webhook secret): every ``github.poll_interval_seconds``
  ask GitHub for issues and comments updated since the last run in every
  repository an agent owns or watches.
* **backfill** (webhook intake): run once at startup to catch what happened
  while the backbone was down.

Both produce the same ``IssueEvent`` objects the webhook produces and hand
them to ``dispatch_event``. Delivery ids are synthesised from the item id and
update time; the ``events`` table dedups them, so overlapping windows and
restarts never double-deliver. The "since" point per repository is the
newest stored event for that repository (or the configured lookback).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from agent_backbone.models import IssueEvent

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github.interface import GitHubClient
    from agent_backbone.services.routing import DeliveryService, DispatchService

log = logging.getLogger(__name__)

_OVERLAP = timedelta(minutes=2)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def issue_event_from_api(
    item: dict[str, Any], repo_full_name: str, since: str
) -> IssueEvent | None:
    """Turn an issues-list item into the event the webhook would have sent."""
    if "pull_request" in item:
        return None
    updated = item.get("updated_at", "")
    created = item.get("created_at", "")
    state = item.get("state", "open")

    if state == "closed":
        action = "closed"
    elif created and created >= since:
        action = "opened"
    else:
        action = "labeled"

    delivery_id = f"poll:{repo_full_name}#{item.get('number')}@{updated}"
    payload = {"action": action, "issue": item, "repository": {"full_name": repo_full_name}}
    return IssueEvent.from_webhook("issues", action, payload, delivery_id)


def comment_event_from_api(
    comment: dict[str, Any], issue: dict[str, Any], repo_full_name: str
) -> IssueEvent:
    delivery_id = f"poll:comment:{comment.get('id')}"
    payload = {
        "action": "created",
        "issue": issue,
        "comment": comment,
        "repository": {"full_name": repo_full_name},
    }
    return IssueEvent.from_webhook("issue_comment", "created", payload, delivery_id)


def issue_number_from_url(url: str) -> int | None:
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, AttributeError):
        return None


class GitHubPoller:
    """Scheduler job that pulls GitHub activity and dispatches it."""

    def __init__(
        self,
        config: BackboneConfig | Callable[[], BackboneConfig],
        db: BackboneDB,
        gh: GitHubClient,
        delivery_svc: DeliveryService,
        dispatch_svc: DispatchService,
    ) -> None:
        self._config_provider = config if callable(config) else (lambda: config)
        self._db = db
        self._gh = gh
        self._delivery_svc = delivery_svc
        self._dispatch_svc = dispatch_svc
        self._since: dict[str, str] = {}

    @property
    def _config(self) -> BackboneConfig:
        return self._config_provider()

    async def _since_for(self, repo: str, last_events: dict[str, str]) -> str:
        if repo in self._since:
            return self._since[repo]
        last = last_events.get(repo)
        if last:
            try:
                return _iso(_parse(last) - _OVERLAP)
            except ValueError:
                pass
        lookback = timedelta(hours=self._config.github.backfill_lookback_hours)
        return _iso(datetime.now(UTC) - lookback)

    async def run(self) -> dict[str, int]:
        from agent_backbone.services.routing._ingest import dispatch_event

        config = self._config
        summary: dict[str, int] = {}
        try:
            last_events = await self._db.last_event_time_by_repo()
        except Exception:
            log.exception("Could not read last event times (using lookback)")
            last_events = {}

        for repo in config.agents.repos:
            since = await self._since_for(repo, last_events)
            newest = since
            try:
                issues = await self._gh.list_issues_since(repo, since)
                comments = await self._gh.list_comments_since(repo, since)
            except Exception:
                log.exception("GitHub poll failed for %s (non-fatal)", repo)
                continue

            issue_cache = {int(item["number"]): item for item in issues if "number" in item}
            events: list[IssueEvent] = []
            for item in issues:
                event = issue_event_from_api(item, repo, since)
                if event is not None:
                    events.append(event)
                newest = max(newest, item.get("updated_at", ""))

            for comment in comments:
                number = issue_number_from_url(comment.get("issue_url", ""))
                if number is None:
                    continue
                issue = issue_cache.get(number)
                if issue is None:
                    try:
                        issue = await self._gh.get_issue_raw(number, repo)
                    except Exception:
                        log.warning("Could not fetch %s#%d for comment (skipped)", repo, number)
                        continue
                    issue_cache[number] = issue
                events.append(comment_event_from_api(comment, issue, repo))
                newest = max(newest, comment.get("updated_at", comment.get("created_at", "")))

            had_errors = False
            for event in events:
                if self._db.is_duplicate(event.delivery_id, config.backbone.max_delivery_ids):
                    summary["deduped"] = summary.get("deduped", 0) + 1
                    continue
                try:
                    outcome = await dispatch_event(
                        event, config, self._db, self._gh, self._delivery_svc, self._dispatch_svc
                    )
                    key = outcome.split(":", 1)[0]
                    summary[key] = summary.get(key, 0) + 1
                except Exception:
                    log.exception("Dispatch failed for polled event %s", event.delivery_id)
                    self._db.forget_delivery(event.delivery_id)
                    had_errors = True
                    summary["errors"] = summary.get("errors", 0) + 1

            # Only advance the cursor when every event dispatched; otherwise the
            # next poll refetches the window and the events table dedups the rest.
            if newest > since and not had_errors:
                self._since[repo] = _iso(_parse(newest) + timedelta(seconds=1))
            else:
                self._since[repo] = since

        if summary:
            log.info("GitHub poll: %s", summary)
        return summary
