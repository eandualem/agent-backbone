"""GitHub polling connector — the no-webhook path.

Every ``poll_interval_seconds`` the poller asks GitHub for issues and comments
updated since the last run in the coordination repository and in every
repository owned by an agent, turns them into the same ``IssueEvent`` objects
the webhook produces, and hands them to the routing layer. Delivery ids are
synthesised from the item's id and update time and recorded in the dedup log,
so restarts and overlapping windows do not double-deliver.

The checkpoint (``since`` per repository) is a small JSON file in the data dir.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_backbone.models import IssueEvent

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github.interface import GitHubClient
    from agent_backbone.services.routing import DeliveryService, DispatchService

log = logging.getLogger(__name__)

CHECKPOINT_FILENAME = "github-poll.json"
_INITIAL_LOOKBACK = timedelta(minutes=5)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def polled_repos(config: BackboneConfig) -> list[str]:
    """Coordination repo plus every agent-owned repo, deduplicated."""
    repos: list[str] = []
    if config.github.repo:
        repos.append(config.github.repo)
    for spec in config.agents:
        if spec.repo and spec.repo not in repos:
            repos.append(spec.repo)
    return repos


class PollCheckpoint:
    """``since`` timestamps per repository, persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._since: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            self._since = {str(k): str(v) for k, v in raw.get("since", {}).items()}
        except (OSError, ValueError, AttributeError):
            self._since = {}

    def since(self, repo: str) -> str:
        return self._since.get(repo) or _iso(datetime.now(UTC) - _INITIAL_LOOKBACK)

    def advance(self, repo: str, value: str) -> None:
        self._since[repo] = value

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"since": self._since}, indent=2))
        os.replace(tmp, self._path)


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
        # Edited/labelled issue. The dispatcher's claims and dedup make
        # re-delivery of an already-delivered issue a no-op.
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
        config: BackboneConfig,
        db: BackboneDB,
        gh: GitHubClient,
        delivery_svc: DeliveryService,
        dispatch_svc: DispatchService,
        checkpoint_path: Path | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._gh = gh
        self._delivery_svc = delivery_svc
        self._dispatch_svc = dispatch_svc
        self._checkpoint = PollCheckpoint(checkpoint_path or config.data_dir / CHECKPOINT_FILENAME)

    async def run(self) -> dict[str, int]:
        from agent_backbone.services.routing._ingest import dispatch_event

        summary: dict[str, int] = {}
        for repo in polled_repos(self._config):
            since = self._checkpoint.since(repo)
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
                        log.warning("Could not fetch issue #%d for comment (skipped)", number)
                        continue
                    issue_cache[number] = issue
                events.append(comment_event_from_api(comment, issue, repo))
                newest = max(newest, comment.get("updated_at", comment.get("created_at", "")))

            for event in events:
                if self._db.is_duplicate(event.delivery_id, self._config.backbone.max_delivery_ids):
                    summary["deduped"] = summary.get("deduped", 0) + 1
                    continue
                if await self._db.is_duplicate_delivery_id(event.delivery_id):
                    summary["deduped"] = summary.get("deduped", 0) + 1
                    continue
                try:
                    outcome = await dispatch_event(
                        event,
                        self._config,
                        self._db,
                        self._gh,
                        self._delivery_svc,
                        self._dispatch_svc,
                    )
                    key = outcome.split(":", 1)[0]
                    summary[key] = summary.get(key, 0) + 1
                except Exception:
                    log.exception("Dispatch failed for polled event %s", event.delivery_id)
                    summary["errors"] = summary.get("errors", 0) + 1

            if newest > since:
                # Nudge by one second so the newest item is not re-fetched forever.
                self._checkpoint.advance(repo, _iso(_parse(newest) + timedelta(seconds=1)))
            elif not self._checkpoint._since.get(repo):
                self._checkpoint.advance(repo, since)

        self._checkpoint.save()
        if summary:
            log.info("GitHub poll: %s", summary)
        return summary
