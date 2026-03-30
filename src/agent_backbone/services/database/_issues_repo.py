"""Issue projection persistence and query operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from agent_backbone.models import CommentData, IssueData, ParsedLabels
from agent_backbone.services.database.models import IssueCommentORM, IssueORM


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _labels_from_json(raw: str) -> ParsedLabels:
    try:
        label_names = json.loads(raw)
    except json.JSONDecodeError:
        label_names = []
    if not isinstance(label_names, list):
        label_names = []
    return ParsedLabels.from_github_labels(
        [{"name": label} for label in label_names if isinstance(label, str)]
    )


def _issue_row_to_summary(row: IssueORM) -> dict:
    labels = _labels_from_json(row.labels_json)
    return {
        "repo_full_name": row.repo_full_name,
        "number": row.issue_number,
        "title": row.title,
        "state": row.state,
        "html_url": row.html_url,
        "labels": labels.model_dump(mode="json"),
        "priority_score": row.priority_score,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "comment_count": row.comment_count,
        "user_login": row.user_login,
    }


def _issue_row_to_detail(row: IssueORM) -> dict:
    return {
        **_issue_row_to_summary(row),
        "body": row.body or "",
    }


def _comment_row_to_dict(row: IssueCommentORM) -> dict:
    return {
        "id": row.id,
        "body": row.body,
        "user_login": row.user_login,
        "from_entity": row.from_entity,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _matches_filters(
    summary: dict,
    *,
    for_entity: str | None,
    from_entity: str | None,
    issue_type: str | None,
    label: str | None,
) -> bool:
    labels = summary.get("labels", {})
    all_labels = labels.get("all_labels", []) if isinstance(labels, dict) else []
    if for_entity and f"for:{for_entity}" not in all_labels:
        return False
    if from_entity and f"from:{from_entity}" not in all_labels:
        return False
    if issue_type and labels.get("issue_type") != issue_type:
        return False
    if label and label not in all_labels:
        return False
    return True


class IssuesRepo:
    """Query repository for canonical issue and comment projections."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def upsert_issue(
        self,
        issue: IssueData,
        *,
        priority_score: float,
        synced_at: str | None = None,
    ) -> dict:
        now = synced_at or _now()
        async with self._session_factory() as session:
            result = await session.execute(
                select(IssueORM).where(
                    IssueORM.repo_full_name == issue.repo_full_name,
                    IssueORM.issue_number == issue.number,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = IssueORM(
                    repo_full_name=issue.repo_full_name,
                    issue_number=issue.number,
                )
                session.add(row)

            row.title = issue.title
            row.body = issue.body
            row.state = issue.state
            row.html_url = issue.html_url
            row.user_login = issue.user_login
            row.labels_json = json.dumps(issue.labels.all_labels)
            row.comment_count = issue.comment_count
            row.priority_score = priority_score
            row.created_at = issue.created_at
            row.updated_at = issue.updated_at
            row.synced_at = now
            await session.commit()
            await session.refresh(row)
            return _issue_row_to_detail(row)

    async def upsert_comment(self, comment: CommentData, *, from_entity: str | None) -> dict:
        async with self._session_factory() as session:
            result = await session.execute(
                select(IssueCommentORM).where(IssueCommentORM.id == comment.id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = IssueCommentORM(id=comment.id)
                session.add(row)

            row.repo_full_name = comment.repo_full_name
            row.issue_number = comment.issue_number
            row.body = comment.body
            row.user_login = comment.user_login
            row.from_entity = from_entity
            row.created_at = comment.created_at
            row.updated_at = comment.updated_at
            await session.commit()
            await session.refresh(row)
            return _comment_row_to_dict(row)

    async def get_issue(self, repo_full_name: str, issue_number: int) -> dict | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(IssueORM).where(
                    IssueORM.repo_full_name == repo_full_name,
                    IssueORM.issue_number == issue_number,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _issue_row_to_detail(row)

    async def list_comments(self, repo_full_name: str, issue_number: int) -> list[dict]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(IssueCommentORM)
                .where(
                    IssueCommentORM.repo_full_name == repo_full_name,
                    IssueCommentORM.issue_number == issue_number,
                )
                .order_by(IssueCommentORM.created_at.asc(), IssueCommentORM.id.asc())
            )
            rows = result.scalars().all()
            return [_comment_row_to_dict(row) for row in rows]

    async def count_open_issues(self, repo_full_name: str | None = None) -> int:
        async with self._session_factory() as session:
            stmt = select(func.count()).select_from(IssueORM).where(IssueORM.state == "open")
            if repo_full_name:
                stmt = stmt.where(IssueORM.repo_full_name == repo_full_name)
            result = await session.execute(stmt)
            return int(result.scalar_one() or 0)

    async def list_issues(
        self,
        *,
        state: str = "open",
        repo_full_name: str | None = None,
        for_entity: str | None = None,
        from_entity: str | None = None,
        issue_type: str | None = None,
        label: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict], int]:
        async with self._session_factory() as session:
            stmt = select(IssueORM)
            if state != "all":
                stmt = stmt.where(IssueORM.state == state)
            if repo_full_name:
                stmt = stmt.where(IssueORM.repo_full_name == repo_full_name)
            result = await session.execute(stmt)
            rows = result.scalars().all()

        summaries = [_issue_row_to_summary(row) for row in rows]
        filtered = [
            summary
            for summary in summaries
            if _matches_filters(
                summary,
                for_entity=for_entity,
                from_entity=from_entity,
                issue_type=issue_type,
                label=label,
            )
        ]
        filtered.sort(
            key=lambda issue: (
                -float(issue["priority_score"]),
                issue["repo_full_name"],
                issue["number"],
            )
        )

        total = len(filtered)
        start = max(page - 1, 0) * per_page
        end = start + per_page
        return filtered[start:end], total

    async def get_issues_by_numbers(
        self,
        repo_full_name: str,
        issue_numbers: list[int],
    ) -> list[dict]:
        if not issue_numbers:
            return []

        async with self._session_factory() as session:
            result = await session.execute(
                select(IssueORM).where(
                    IssueORM.repo_full_name == repo_full_name,
                    IssueORM.issue_number.in_(issue_numbers),
                )
            )
            rows = result.scalars().all()

        summaries = [_issue_row_to_summary(row) for row in rows]
        order = {number: idx for idx, number in enumerate(issue_numbers)}
        summaries.sort(key=lambda issue: order.get(issue["number"], len(order)))
        return summaries
