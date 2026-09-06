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
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    enabled: bool = True

    def __post_init__(self) -> None:
        self.status = JobStatus(name=self.name, interval_seconds=self.interval)


class PeriodicScheduler:
    """LifecycleAware scheduler for interval jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._running = False

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
        job = _Job(name, float(interval_seconds), fn, run_immediately, once)
        self._jobs[name] = job
        if self._running:
            job.task = asyncio.create_task(self._loop(job), name=f"scheduler-{job.name}")

    def configure(
        self,
        name: str,
        interval_seconds: float,
        fn: JobFn,
        *,
        enabled: bool = True,
        run_immediately: bool = False,
    ) -> None:
        """Change scheduling without cancelling a run that may be delivering text."""
        if interval_seconds <= 0:
            raise ValueError(f"Job {name}: interval must be positive")
        job = self._jobs.get(name)
        if job is None:
            if enabled:
                self.add(name, interval_seconds, fn, run_immediately=run_immediately)
            return
        changed = job.interval != interval_seconds or job.enabled != enabled
        job.interval = float(interval_seconds)
        job.status.interval_seconds = job.interval
        job.fn = fn
        job.enabled = enabled
        if changed:
            job.wake.set()
        if self._running and enabled and (job.task is None or job.task.done()):
            job.run_immediately = run_immediately
            job.task = asyncio.create_task(self._loop(job), name=f"scheduler-{job.name}")

    @property
    def jobs(self) -> list[JobStatus]:
        return [job.status for job in self._jobs.values() if job.enabled]

    # --- LifecycleAware ---

    async def start(self) -> None:
        self._running = True
        for job in self._jobs.values():
            if not job.enabled or (job.task is not None and not job.task.done()):
                continue
            job.task = asyncio.create_task(self._loop(job), name=f"scheduler-{job.name}")
        log.info("Scheduler started with %d job(s): %s", len(self._jobs), ", ".join(self._jobs))

    async def stop(self) -> None:
        self._running = False
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
            if job.enabled
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
                if job.enabled
            },
        }

    # --- Internals ---

    async def _wait(self, job: _Job) -> None:
        while job.enabled:
            job.wake.clear()
            try:
                await asyncio.wait_for(job.wake.wait(), timeout=job.interval)
            except TimeoutError:
                return

    async def _loop(self, job: _Job) -> None:
        if not job.run_immediately:
            await self._wait(job)
        while job.enabled:
            await self._run_once(job)
            if job.once:
                return
            await self._wait(job)

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
