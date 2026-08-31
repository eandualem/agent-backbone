"""Tests for the in-process periodic scheduler."""

from __future__ import annotations

import asyncio

import pytest

from agent_backbone.services.scheduler import PeriodicScheduler


class TestPeriodicScheduler:
    async def test_runs_jobs_on_interval(self):
        scheduler = PeriodicScheduler()
        calls: list[int] = []

        async def job():
            calls.append(1)
            return len(calls)

        scheduler.add("tick", 0.02, job, run_immediately=True)
        await scheduler.start()
        await asyncio.sleep(0.09)
        await scheduler.stop()

        assert len(calls) >= 2
        status = scheduler.jobs[0]
        assert status.runs == len(calls)
        assert status.failures == 0
        assert status.last_result == len(calls)

    async def test_failures_are_recorded_and_loop_continues(self):
        scheduler = PeriodicScheduler()
        runs = 0

        async def flaky():
            nonlocal runs
            runs += 1
            if runs == 1:
                raise RuntimeError("boom")

        scheduler.add("flaky", 0.02, flaky, run_immediately=True)
        await scheduler.start()
        await asyncio.sleep(0.07)
        await scheduler.stop()

        status = scheduler.jobs[0]
        assert runs >= 2
        assert status.failures == 1
        assert status.last_error is None  # last run succeeded

    async def test_overlapping_runs_are_skipped(self):
        scheduler = PeriodicScheduler()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow():
            started.set()
            await release.wait()
            return "done"

        scheduler.add("slow", 60, slow)
        task = asyncio.create_task(scheduler.run_now("slow"))
        await started.wait()
        assert await scheduler.run_now("slow") is None  # skipped while running
        release.set()
        assert await task == "done"
        assert scheduler.jobs[0].runs == 1

    async def test_health_and_validation(self):
        scheduler = PeriodicScheduler()

        async def noop():
            return None

        scheduler.add("a", 10, noop)
        with pytest.raises(ValueError):
            scheduler.add("a", 10, noop)
        with pytest.raises(ValueError):
            scheduler.add("b", 0, noop)

        health = await scheduler.health_check()
        assert health["healthy"] is False  # not started yet
        await scheduler.start()
        assert (await scheduler.health_check())["healthy"] is True
        await scheduler.stop()
