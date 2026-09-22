"""Short-lived command processes must not outlive cancelled callers."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent_backbone.git import run_git
from agent_backbone.services.terminal._core import _run_tmux


@pytest.mark.parametrize("command", ["git", "tmux"])
@pytest.mark.parametrize("cancel", [True, False])
async def test_interrupted_command_reaps_process(command, cancel, caplog):
    started = asyncio.Event()
    proc = Mock(returncode=None)

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

    proc.kill.assert_called_once()
    assert proc.communicate.await_count == 2
    assert "private-value" not in caplog.text
