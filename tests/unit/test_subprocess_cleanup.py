"""Short-lived command processes must not outlive cancelled callers."""

import asyncio
import os
import signal
import sys
from contextlib import suppress
from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent_backbone.git import run_git
from agent_backbone.services.terminal._core import _run_tmux


@pytest.mark.parametrize("command", ["git", "tmux"])
@pytest.mark.parametrize("cancel", [True, False])
async def test_interrupted_command_reaps_process(command, cancel, caplog):
    started = asyncio.Event()
    proc = Mock(returncode=None, pid=1234)

    async def communicate(input=None):
        if proc.returncode is None:
            started.set()
            await asyncio.Future()
        return b"", b""

    def kill():
        proc.returncode = -9

    proc.communicate = AsyncMock(side_effect=communicate)
    proc.kill.side_effect = kill
    timeout = asyncio.timeout
    call = (
        run_git(".", "status")
        if command == "git"
        else _run_tmux("new-session", "-e", "TEST_SECRET=private-value")
    )
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.timeout", side_effect=lambda _: timeout(30 if cancel else 0)),
        patch("os.killpg", side_effect=lambda *_: kill()) as kill_group,
    ):
        task = asyncio.create_task(call)
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            code, _, error = await task
            assert code != 0 and error

    if command == "git":
        kill_group.assert_called_once_with(proc.pid, signal.SIGKILL)
    else:
        proc.kill.assert_called_once()
    assert proc.communicate.await_count == 2
    assert "private-value" not in caplog.text


async def test_git_timeout_closes_pipes_inherited_by_a_child():
    create = asyncio.subprocess.create_subprocess_exec
    processes = []
    child = "import time; time.sleep(30)"
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "print('ready', flush=True); time.sleep(30)"
    )

    async def spawn(*args, **kwargs):
        assert kwargs.get("start_new_session"), "scratch children need their own process group"
        proc = await create(sys.executable, "-c", parent, **kwargs)
        processes.append(proc)
        await proc.stdout.readline()
        return proc

    try:
        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            code, _, error = await asyncio.wait_for(run_git(".", "status", timeout=0.1), 3)
        assert code == 1 and "timed out" in error
    finally:
        for proc in processes:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.communicate()
