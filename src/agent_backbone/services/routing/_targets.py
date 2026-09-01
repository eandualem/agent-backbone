"""Who hears about a GitHub event, and what an agent's queue is.

Per repository, four relationships decide routing:

- **owner** — the agent whose directory *is* the repo: unlabelled issues
  are its work (sole owner) or announced to all owners (several).
- **``for:<agent>``** — an explicit target in any repo the agent owns or
  watches: goes to that agent's queue.
- **``from:<agent>``** — the opener: replies (comments, close) come back.
- **watch** — informational notifications only, never queued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_backbone.models import EventType, IssueData, IssueEvent
from agent_backbone.services.routing._priority import compute_priority_score

if TYPE_CHECKING:
    from agent_backbone.config import AgentSpec, BackboneConfig


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def issue_repo(issue: IssueData) -> str:
    return issue.repo_full_name or ""


def agent_knows_repo(spec: AgentSpec, repo: str) -> bool:
    return any(_same(r, repo) for r in spec.repos)


@dataclass
class EventRouting:
    """Resolved audiences for one event."""

    repo: str
    queue: list[str] = field(default_factory=list)
    """Agents whose queue receives the issue (``for:`` targets or the sole owner)."""
    announce: list[str] = field(default_factory=list)
    """Owners of a multi-owner repo told about an unassigned issue (not queued)."""
    watch: list[str] = field(default_factory=list)
    """Watchers told for information only."""


def route_issue_event(event: IssueEvent, config: BackboneConfig) -> EventRouting:
    """Audiences for an issue/PR event (comments are routed separately)."""
    repo = issue_repo(event.issue)
    routing = EventRouting(repo=repo)
    agents = config.agents
    ignore = config.routing.ignore_targets
    owners = [s.name for s in agents.owners(repo)] if repo else []
    watchers = [s.name for s in agents.watchers(repo)] if repo else []

    explicit = [t for t in event.issue.labels.targets if t not in ignore]
    if event.event_type == EventType.PULL_REQUEST_OPENED:
        routing.watch = [n for n in owners + watchers if n not in explicit]
        routing.queue = [t for t in explicit if t in agents]
        return routing

    if explicit:
        routing.queue = [
            t for t in explicit if t in agents and agent_knows_repo(agents.get(t), repo)
        ]
        # for: targets that don't know the repo still get it: explicit wins.
        routing.queue += [t for t in explicit if t in agents and t not in routing.queue]
    elif event.event_type == EventType.ISSUE_OPENED:
        if len(owners) == 1:
            routing.queue = owners
        elif owners:
            routing.announce = owners
    # ISSUE_LABELED without for: labels is an edit — nobody is queued.

    routing.watch = [n for n in watchers if n not in routing.queue and n not in routing.announce]
    return routing


def comment_audience(event: IssueEvent, commenter: str | None, config: BackboneConfig) -> list[str]:
    """Agents notified about a comment: opener ∪ targets ∪ sole owner, minus the commenter."""
    routing = route_issue_event(
        IssueEvent(event_type=EventType.ISSUE_OPENED, issue=event.issue), config
    )
    parties: set[str] = set(routing.queue)
    sender = event.issue.labels.sender
    if sender and sender != "unknown" and sender in config.agents:
        parties.add(sender)
    parties -= set(config.routing.ignore_targets)
    if commenter:
        parties.discard(commenter)
    return sorted(parties)


def _has_for_label(issue: IssueData) -> bool:
    return bool(issue.labels.targets)


async def list_open_queue_for_target(
    config: BackboneConfig, target: str, gh: object
) -> list[IssueData]:
    """An agent's open queue across every repository it owns or watches.

    ``for:<target>`` issues everywhere it looks, plus every unlabelled open
    issue in a repository it is the *sole* owner of — highest priority first.
    """
    spec = config.agents.get(target)
    if spec is None or gh is None:
        return []

    issues: list[IssueData] = []
    seen: set[tuple[str, int]] = set()

    def _add(items: list[IssueData]) -> None:
        for item in items:
            key = (issue_repo(item).casefold(), item.number)
            if key not in seen:
                seen.add(key)
                issues.append(item)

    for repo in spec.repos:
        _add(await gh.list_issues(state="open", labels=[f"for:{target}"], repo_full_name=repo))

    if spec.repo and len(config.agents.owners(spec.repo)) == 1:
        unlabelled = [
            item
            for item in await gh.list_issues(state="open", repo_full_name=spec.repo)
            if not _has_for_label(item)
        ]
        _add(unlabelled)

    scoring = config.priority_scoring
    issues.sort(key=lambda issue: (-compute_priority_score(issue, scoring), issue.number))
    return issues


def queue_scope(issues: list[IssueData]) -> set[tuple[str, int]]:
    return {(issue_repo(i), i.number) for i in issues}
