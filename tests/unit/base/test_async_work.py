"""Cancellation retains threads and native child handles until actual exit."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_backbone.base import owned_process, run_thread


@pytest.mark.parametrize("fail", [False, True])
async def test_repeated_cancellation_waits_for_worker_exit(fail):
    entered, release = threading.Event(), threading.Event()
    exited = threading.Event()

    def work():
        entered.set()
        release.wait(3)
        exited.set()
        if fail:
            raise ValueError("worker failed")
        return 42

    task = asyncio.create_task(run_thread(work))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        assert not exited.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert exited.is_set()


async def test_cancellation_during_spawn_retains_child_through_reaping():
    spawning, spawned, reaping, reaped = (asyncio.Event() for _ in range(4))
    process = SimpleNamespace(returncode=None, kill=Mock())

    async def spawn():
        spawning.set()
        await spawned.wait()
        return process

    async def communicate():
        reaping.set()
        await reaped.wait()
        process.returncode = -9
        return b"", b""

    process.communicate = communicate

    async def work():
        async with owned_process(spawn()):
            pytest.fail("cancelled creation cannot start its body")

    task = asyncio.create_task(work())
    try:
        await spawning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        spawned.set()
        await asyncio.wait_for(reaping.wait(), 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        process.kill.assert_called_once()
    finally:
        spawned.set()
        reaped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9


async def test_thread_success_and_failure_are_preserved():
    assert await run_thread(lambda *, n: n, n=42) == 42
    with pytest.raises(ZeroDivisionError):
        await run_thread(lambda: 1 / 0)
