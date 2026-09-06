"""Transport-neutral review lifecycle from check metadata, never comment wording."""

from agent_backbone.models import EventType, IssueEvent, ReviewData


def review_started_event(check: dict, pull: dict, repo: str, delivery_id: str) -> IssueEvent | None:
    if (
        check.get("status") != "in_progress"
        or not check.get("started_at")
        or not check.get("head_sha")
    ):
        return None
    event = IssueEvent.from_webhook(
        "pull_request",
        "opened",
        {"pull_request": pull, "repository": {"full_name": repo}},
        delivery_id,
    )
    event.event_type = EventType.REVIEW_STARTED
    event.review = ReviewData(
        id=check.get("id", 0),
        state="started",
        user_login=(check.get("app") or {}).get("slug", "unknown"),
        commit_id=check["head_sha"],
        submitted_at=check["started_at"],
        head_sha=(pull.get("head") or {}).get("sha", ""),
        html_url=check.get("html_url") or "",
    )
    return event


def review_status_event(
    status: dict, sha: str, pull: dict, repo: str, delivery_id: str
) -> IssueEvent | None:
    """Pending commit statuses are the older equivalent of an active check.

    GitHub pending includes queued work; do not infer a verdict or parse a
    particular review vendor's description.
    """
    if status.get("state") != "pending":
        return None
    reviewer = (status.get("creator") or status.get("sender") or {}).get("login", "unknown")
    event = review_started_event(
        {
            "id": status.get("id", 0),
            "status": "in_progress",
            "head_sha": sha,
            "started_at": status.get("created_at"),
            "app": {"slug": reviewer},
            "html_url": status.get("target_url") or "",
        },
        pull,
        repo,
        delivery_id,
    )
    if event is not None:
        event.review.body = "Reviewer status is pending (queued or running)."
    return event
