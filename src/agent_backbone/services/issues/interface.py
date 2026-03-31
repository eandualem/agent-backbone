"""Canonical issue-domain service."""

from __future__ import annotations

import logging

from agent_backbone.api.issue_updates import emit_issue_change
from agent_backbone.config import BackboneConfig
from agent_backbone.models import CommentData, EventType, IssueData, parse_from_tag
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.issues._priority import compute_priority_score

log = logging.getLogger(__name__)


class IssueService:
    """Owns issue projection refresh, persistence, and socket emission."""

    def __init__(
        self,
        *,
        config: BackboneConfig,
        db: BackboneDB,
        gh: GitHubClient,
        sio=None,
    ) -> None:
        self._config = config
        self._db = db
        self._gh = gh
        self._sio = sio

    async def _repo_inventory(self) -> list[str]:
        """List all repos for the GitHub owner via the GitHub API."""
        return await self._gh.list_owner_repos()

    async def _refresh_issue_projection(
        self,
        issue: IssueData,
        *,
        emit_changes: bool,
    ) -> dict:
        existing = await self._db.issues.get_issue(issue.repo_full_name, issue.number)
        detail = await self._db.issues.upsert_issue(
            issue,
            priority_score=compute_priority_score(issue, self._config.priority_scoring),
        )
        if emit_changes and detail != existing:
            await emit_issue_change(
                self._sio,
                summary=self._to_summary(detail),
                detail=detail,
            )
        return detail

    @staticmethod
    def _to_summary(detail: dict) -> dict:
        return {
            key: value
            for key, value in detail.items()
            if key != "body"
        }

    async def refresh_issue(
        self,
        repo_full_name: str,
        issue_number: int,
        *,
        issue_data: IssueData | None = None,
        emit_changes: bool = True,
    ) -> dict:
        issue = issue_data or await self._gh.get_issue(issue_number, repo_full_name=repo_full_name)
        return await self._refresh_issue_projection(issue, emit_changes=emit_changes)

    async def upsert_comment(self, comment: CommentData) -> dict:
        return await self._db.issues.upsert_comment(
            comment,
            from_entity=parse_from_tag(comment.body) if comment.body else None,
        )

    async def sync_issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict]:
        comments = await self._gh.list_comments(issue_number, repo_full_name=repo_full_name)
        persisted: list[dict] = []
        for comment in comments:
            persisted.append(await self.upsert_comment(comment))
        return persisted

    async def handle_comment_change(
        self,
        repo_full_name: str,
        issue_number: int,
        *,
        comment: CommentData | None = None,
        issue_data: IssueData | None = None,
        emit_changes: bool = True,
    ) -> tuple[dict, dict | None]:
        persisted_comment = await self.upsert_comment(comment) if comment is not None else None
        detail = await self.refresh_issue(
            repo_full_name,
            issue_number,
            issue_data=issue_data,
            emit_changes=emit_changes,
        )
        return detail, persisted_comment

    async def _process_issues(
        self,
        issues: list,
        repo_full_name: str,
        *,
        emit_changes: bool,
        changed: list[dict],
    ) -> None:
        for issue in issues:
            existing = await self._db.issues.get_issue(
                issue.repo_full_name, issue.number
            )
            detail = await self._refresh_issue_projection(
                issue, emit_changes=emit_changes
            )
            if existing is None or existing.get("updated_at") != detail.get(
                "updated_at"
            ):
                if issue.comment_count > 0:
                    try:
                        await self.sync_issue_comments(
                            repo_full_name, issue.number
                        )
                    except Exception:
                        log.warning(
                            "Issue comment sync failed for %s#%d",
                            repo_full_name,
                            issue.number,
                            exc_info=True,
                        )
            if detail != existing:
                changed.append(detail)

    async def sync_repo(
        self, repo_full_name: str, *, emit_changes: bool = True
    ) -> list[dict]:
        """Sync issues for a repo: all open issues first, then recent closed."""
        changed: list[dict] = []

        # Pass 1: ALL open issues (paginated through every page)
        try:
            open_issues = await self._gh.list_issues(
                state="open",
                repo_full_name=repo_full_name,
                per_page=100,
            )
        except Exception:
            log.warning(
                "Open issue sync failed for %s", repo_full_name, exc_info=True
            )
            return changed

        log.info(
            "Syncing %s: %d open issues", repo_full_name, len(open_issues)
        )
        await self._process_issues(
            open_issues, repo_full_name,
            emit_changes=emit_changes, changed=changed,
        )

        # Pass 2: recently-updated closed issues (first page only)
        try:
            closed_issues = await self._gh.list_issues(
                state="closed",
                repo_full_name=repo_full_name,
                per_page=100,
                sort="updated",
                direction="desc",
                max_pages=1,
            )
        except Exception:
            log.warning(
                "Closed issue sync failed for %s",
                repo_full_name,
                exc_info=True,
            )
            return changed

        await self._process_issues(
            closed_issues, repo_full_name,
            emit_changes=emit_changes, changed=changed,
        )

        return changed

    async def sync_inventory(self, *, emit_changes: bool = True) -> list[dict]:
        changed: list[dict] = []
        for repo_full_name in await self._repo_inventory():
            changed.extend(await self.sync_repo(repo_full_name, emit_changes=emit_changes))
        return changed

    async def handle_webhook_event(self, event) -> None:
        if event.issue.is_pull_request:
            return

        if event.event_type in {
            EventType.ISSUE_OPENED,
            EventType.ISSUE_REOPENED,
            EventType.ISSUE_LABELED,
            EventType.ISSUE_CLOSED,
        }:
            await self.refresh_issue(
                event.issue.repo_full_name,
                event.issue.number,
                issue_data=event.issue,
            )
            return

        if event.event_type == EventType.COMMENT_CREATED:
            await self.handle_comment_change(
                event.issue.repo_full_name,
                event.issue.number,
                comment=event.comment,
                issue_data=event.issue,
            )

    async def list_issues(
        self,
        *,
        state: str,
        repo_full_name: str | None,
        for_entity: str | None,
        from_entity: str | None,
        issue_type: str | None,
        label: str | None,
        page: int,
        per_page: int,
    ) -> tuple[list[dict], int]:
        return await self._db.issues.list_issues(
            state=state,
            repo_full_name=repo_full_name,
            for_entity=for_entity,
            from_entity=from_entity,
            issue_type=issue_type,
            label=label,
            page=page,
            per_page=per_page,
        )

    async def get_issue(self, repo_full_name: str, issue_number: int) -> dict | None:
        return await self._db.issues.get_issue(repo_full_name, issue_number)

    async def list_comments(self, repo_full_name: str, issue_number: int) -> list[dict]:
        return await self._db.issues.list_comments(repo_full_name, issue_number)

    async def count_open_issues(self, repo_full_name: str | None = None) -> int:
        return await self._db.issues.count_open_issues(repo_full_name)

    async def create_issue(
        self,
        *,
        repo_full_name: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict:
        issue = await self._gh.create_issue(
            title,
            body,
            labels or [],
            repo_full_name=repo_full_name,
        )
        detail = await self.refresh_issue(
            repo_full_name,
            issue.number,
            issue_data=issue,
        )
        return self._to_summary(detail)

    async def add_comment(
        self,
        *,
        repo_full_name: str,
        issue_number: int,
        body: str,
    ) -> dict:
        comment = await self._gh.add_comment(issue_number, body, repo_full_name=repo_full_name)
        issue = await self._gh.get_issue(issue_number, repo_full_name=repo_full_name)
        _, persisted_comment = await self.handle_comment_change(
            repo_full_name,
            issue_number,
            comment=comment,
            issue_data=issue,
        )
        assert persisted_comment is not None
        return persisted_comment

    async def update_issue_state(
        self,
        *,
        repo_full_name: str,
        issue_number: int,
        state: str,
    ) -> dict:
        issue = await self._gh.update_issue(issue_number, state, repo_full_name=repo_full_name)
        detail = await self.refresh_issue(
            repo_full_name,
            issue_number,
            issue_data=issue,
        )
        return self._to_summary(detail)
