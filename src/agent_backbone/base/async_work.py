"""Keep blocking work inside the lifetime of its owning coroutine."""

import asyncio
import os
import signal
from contextlib import asynccontextmanager, suppress


async def complete_before_cancel(awaitable):
    """Propagate cancellation only after owned work exits, even on repeated cancel."""
    task = asyncio.ensure_future(awaitable)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                return task.result()
            cancelled = cancelled or exc
        except BaseException:
            break
    if cancelled is not None:
        # Retrieve the exception even when cancellation is the caller's result.
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()


async def run_thread(function, /, *args, **kwargs):
    """Await a worker's actual exit before releasing its caller's resources."""
    return await complete_before_cancel(asyncio.to_thread(function, *args, **kwargs))


@asynccontextmanager
async def owned_process(spawn, *, process_group=False):
    """Own subprocess creation and reaping across caller cancellation.

    ``process_group=True`` requires a new process-group leader, such as a child
    spawned with ``start_new_session=True``. Its PID must identify its own group.
    """
    creation = asyncio.ensure_future(spawn)
    completed = False
    cancelled = None

    async def reap():
        process = await creation
        if process_group and not completed:
            # Descendants may retain pipes after their group leader exits.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()
        elif process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()

    try:
        process = await asyncio.shield(creation)
        yield process
        completed = True
    except asyncio.CancelledError as exc:
        cancelled = exc
        raise
    finally:
        # Spawn itself can be cancelled after the OS child exists. Keep its
        # future alive until we can obtain the handle and reap that child.
        try:
            await complete_before_cancel(reap())
        except BaseException:
            if cancelled is not None:
                raise cancelled from None
            raise


async def check_before_effect(check):
    """Revalidate a caller's captured authority at a deferred effect boundary."""
    if check is not None:
        await check()
