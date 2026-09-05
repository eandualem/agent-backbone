"""Tests for the in-process periodic scheduler."""

from __future__ import annotations

import asyncio

import pytest

from agent_backbone.services.scheduler import PeriodicScheduler


class TestPeriodicScheduler:
    @pytest.mark.parametrize("fail", [False, True])
    async def test_one_shot_finishes_after_one_attempt_and_reports_health(self, fail):
        scheduler = PeriodicScheduler()
        calls = 0

        async def backfill():
            nonlocal calls
            calls += 1
            if fail:
                raise RuntimeError("backfill failed")

        scheduler.add("backfill", 0, backfill, run_immediately=True, once=True)
        await scheduler.start()
        try:
            await asyncio.wait_for(asyncio.shield(scheduler._jobs["backfill"].task), timeout=1)
            assert calls == 1
            assert scheduler.jobs[0].runs == 1
            assert scheduler.jobs[0].failures == int(fail)
            assert (await scheduler.health_check())["healthy"] is not fail
        finally:
            await scheduler.stop()

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
        job = scheduler._jobs["slow"]
        task = asyncio.create_task(scheduler._run_once(job))
        await started.wait()
        await scheduler._run_once(job)  # skipped while running
        release.set()
        await task
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
