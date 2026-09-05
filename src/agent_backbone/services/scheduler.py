"""In-process periodic job scheduler.

Jobs are plain coroutines run on a fixed interval inside the API process.
Overlapping runs of the same job are skipped and failures are logged without
stopping the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

JobFn = Callable[[], Awaitable[object]]


@dataclass
class JobStatus:
    name: str
    interval_seconds: float
    runs: int = 0
    failures: int = 0
    last_started: float | None = None
    last_finished: float | None = None
    last_error: str | None = None
    running: bool = False


@dataclass
class _Job:
    name: str
    interval: float
    fn: JobFn
    run_immediately: bool
    once: bool = False
    status: JobStatus = field(init=False)
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.status = JobStatus(name=self.name, interval_seconds=self.interval)


class PeriodicScheduler:
    """LifecycleAware scheduler for interval jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}

    def add(
        self,
        name: str,
        interval_seconds: float,
        fn: JobFn,
        *,
        run_immediately: bool = False,
        once: bool = False,
    ) -> None:
        """Register a job before start; ``once`` finishes after its first attempt."""
        if name in self._jobs:
            raise ValueError(f"Job already registered: {name}")
        if interval_seconds < 0 or (interval_seconds == 0 and not once):
            raise ValueError(f"Job {name}: interval must be positive")
        self._jobs[name] = _Job(name, float(interval_seconds), fn, run_immediately, once)

    @property
    def jobs(self) -> list[JobStatus]:
        return [job.status for job in self._jobs.values()]

    # --- LifecycleAware ---

    async def start(self) -> None:
        for job in self._jobs.values():
            job.task = asyncio.create_task(self._loop(job), name=f"scheduler-{job.name}")
        log.info("Scheduler started with %d job(s): %s", len(self._jobs), ", ".join(self._jobs))

    async def stop(self) -> None:
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for job in self._jobs.values():
            job.task = None

    async def health_check(self) -> dict:
        alive = all(
            job.task is not None
            and (
                not job.task.done()
                or (
                    job.once
                    and not job.task.cancelled()
                    and job.task.exception() is None
                    and job.status.last_error is None
                )
            )
            for job in self._jobs.values()
        )
        return {
            "healthy": alive or not self._jobs,
            "service": "scheduler",
            "jobs": {
                job.name: {
                    "runs": job.status.runs,
                    "failures": job.status.failures,
                    "running": job.status.running,
                    "last_error": job.status.last_error,
                }
                for job in self._jobs.values()
            },
        }

    # --- Internals ---

    async def _loop(self, job: _Job) -> None:
        if not job.run_immediately:
            await asyncio.sleep(job.interval)
        while True:
            await self._run_once(job)
            if job.once:
                return
            await asyncio.sleep(job.interval)

    async def _run_once(self, job: _Job) -> None:
        if job.lock.locked():
            log.debug("Job %s still running — skipping this tick", job.name)
            return
        async with job.lock:
            status = job.status
            status.running = True
            status.last_started = time.time()
            try:
                await job.fn()
                status.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status.failures += 1
                status.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Job %s failed", job.name)
            finally:
                status.runs += 1
                status.running = False
                status.last_finished = time.time()
