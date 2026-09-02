"""Sub-issue dependencies — parent/child links per repository."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


class DependencyRepo(Repo):
    async def parents(self, sub_issue_number: int, *, repo: str = "") -> list[int]:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT parent_number FROM issue_dependencies"
                    " WHERE repo = :repo AND sub_issue_number = :sub"
                ),
                {"repo": repo, "sub": sub_issue_number},
            )
            return [row._mapping["parent_number"] for row in result.fetchall()]

    async def sync(self, parent: int, sub_issues: list[int], *, repo: str = "") -> None:
        async with self._tx() as conn:
            now = now_iso()
            for sub in sub_issues:
                await conn.execute(
                    text(
                        """INSERT INTO issue_dependencies
                           (repo, parent_number, sub_issue_number, updated_at)
                           VALUES (:repo, :parent, :sub, :now)
                           ON CONFLICT(repo, parent_number, sub_issue_number) DO UPDATE SET
                             updated_at = excluded.updated_at"""
                    ),
                    {"repo": repo, "parent": parent, "sub": sub, "now": now},
                )
            if sub_issues:
                placeholders = ",".join(f":sub_{i}" for i in range(len(sub_issues)))
                params: dict[str, object] = {"repo": repo, "parent": parent}
                for i, sub in enumerate(sub_issues):
                    params[f"sub_{i}"] = sub
                await conn.execute(
                    text(
                        "DELETE FROM issue_dependencies"
                        " WHERE repo = :repo AND parent_number = :parent"
                        f" AND sub_issue_number NOT IN ({placeholders})"
                    ),
                    params,
                )
            else:
                await conn.execute(
                    text(
                        "DELETE FROM issue_dependencies"
                        " WHERE repo = :repo AND parent_number = :parent"
                    ),
                    {"repo": repo, "parent": parent},
                )
