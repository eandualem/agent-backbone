"""Own API work that continues after its requesting client leaves."""

import asyncio
import logging
from contextlib import AsyncExitStack

from fastapi import HTTPException

from agent_backbone.api.profile_binding import provider_context_current

log = logging.getLogger(__name__)


class ApiTasks:
    """Drain accepted work before the application's dependencies stop."""

    def __init__(self):
        self._tasks: set[asyncio.Task] = set()
        self.closing = False

    def create(self, run, *, config, db, provider):
        if self.closing:
            raise HTTPException(status_code=503, detail="Application is shutting down")
        task = asyncio.create_task(self._run(run, config, db, provider))
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    async def _run(self, run, config, db, provider):
        async with AsyncExitStack() as operations:
            if provider is not None:
                await operations.enter_async_context(provider.operation())
                if not await provider_context_current(provider, config, db):
                    raise ValueError("Background API provider context does not match")
            return await run()

    def _finished(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.warning("Background API task failed: %s", type(error).__name__)

    def close_admission(self):
        self.closing = True

    async def start(self):
        await self.stop()
        self.closing = False

    async def stop(self):
        self.close_admission()
        while self._tasks:
            # Unlike gather, wait never propagates shutdown cancellation to the
            # workers. A later stop can finish draining the same owned tasks.
            await asyncio.wait(tuple(self._tasks))
            await asyncio.sleep(0)

    async def health_check(self):
        return {"healthy": not self.closing}
