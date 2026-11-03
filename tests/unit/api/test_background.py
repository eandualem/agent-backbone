"""Detached API ownership survives lifecycle stop/start and cancelled drains."""

import asyncio
from unittest.mock import AsyncMock

from agent_backbone.api.background import ApiTasks
from agent_backbone.api.validation import Validations


async def test_notice_skipped_during_shutdown_is_scheduled_once_after_restart(monkeypatch):
    tasks = ApiTasks()
    validations = Validations(tasks)
    notice = AsyncMock()
    monkeypatch.setattr(validations, "_notice", notice)
    tasks.close_admission()
    validations.failed(None, "worker", "first", "unknown_confirmation", 0)
    notice.assert_not_awaited()
    await tasks.start()
    validations.failed(None, "worker", "second", "unknown_confirmation", 1)
    validations.failed(None, "worker", "third", "unknown_confirmation", 2)
    await tasks.stop()
    notice.assert_awaited_once()
    incident = notice.call_args.args[2]
    assert incident.checked == 3
    assert incident.reasons == {"unknown_confirmation": 3}
    await tasks.start()
    validations.failed(None, "worker", "fourth", "unknown_confirmation", 3)
    await tasks.stop()
    notice.assert_awaited_once()


async def test_restarting_task_owner_drains_previous_work_before_reopening():
    tasks = ApiTasks()
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await release.wait()

    await tasks.start()
    previous = tasks.create(work, config=None, db=None, provider=None)
    await entered.wait()
    stopping = asyncio.create_task(tasks.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.gather(stopping, return_exceptions=True)
    restarting = asyncio.create_task(tasks.start())
    try:
        await asyncio.sleep(0)
        assert tasks.closing
        assert not restarting.done()
        assert not previous.done()
        release.set()
        await asyncio.wait_for(restarting, 2)
        assert previous.done()
        assert not tasks.closing
        fresh = AsyncMock(return_value="new lifespan")
        assert await tasks.create(fresh, config=None, db=None, provider=None) == "new lifespan"
        fresh.assert_awaited_once()
        await tasks.stop()
        await tasks.start()
        assert not tasks.closing
    finally:
        release.set()
        await asyncio.gather(restarting, previous, return_exceptions=True)
        await tasks.stop()
