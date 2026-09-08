"""A short-lived complete repository view for one job tick or API request.

Only open queue lists and dependency reads are shared. Individual issue reads
still reach GitHub so delivery retirement checks do not use stale state.
"""

from __future__ import annotations

import asyncio


class QueueSnapshot:
    def __init__(self, client, *, concurrency: int = 8) -> None:
        self.client = client
        self._limit = asyncio.Semaphore(concurrency)
        self._lists: dict[tuple, asyncio.Task] = {}

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def _read(self, key, fetch):
        async def limited():
            async with self._limit:
                return await fetch()

        if key not in self._lists:
            self._lists[key] = asyncio.create_task(limited())
        # Failure stays in this snapshot, avoiding repeated calls to a failing
        # repo during a tick. The next tick starts fresh.
        return await self._lists[key]

    async def list_issues(self, *, state="open", labels=None, repo_full_name, **kwargs):
        if state != "open" or any(not label.startswith("for:") for label in labels or ()):
            # Arbitrary label queries keep the client's filters and pagination.
            return await self.client.list_issues(
                state=state, labels=labels, repo_full_name=repo_full_name, **kwargs
            )
        items = await self._read(
            ("open", repo_full_name.casefold()),
            lambda: self.client.list_issues(
                state="open", repo_full_name=repo_full_name, all_pages=True
            ),
        )
        if labels:
            items = [i for i in items if all(label[4:] in i.labels.targets for label in labels)]
        return list(items)

    async def get_sub_issues(self, number, *, repo_full_name):
        return await self._read(
            ("subs", repo_full_name.casefold(), number),
            lambda: self.client.get_sub_issues(number, repo_full_name=repo_full_name),
        )
