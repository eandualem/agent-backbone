"""Manual real-tmux check: ``make smoke``. No backbone service or model required."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import uuid

from agent_backbone.services.terminal import (
    capture_pane,
    paste_message,
    query_format_vars,
    send_keys,
    start_session,
    stop_session,
)


async def wait_for_output(session: str, expected: str) -> None:
    """Wait for the child process to print its acknowledgement."""
    async with asyncio.timeout(5):
        while expected not in (await capture_pane(session) or ""):
            await asyncio.sleep(0.05)


async def main() -> None:
    session = f"backbone-smoke-{uuid.uuid4().hex}"
    marker = uuid.uuid4().hex
    program = (
        "import sys\n"
        "print('smoke-ready', flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('received:' + line.strip(), flush=True)\n"
    )
    with tempfile.TemporaryDirectory(prefix="backbone-smoke-") as directory:
        try:
            if not await start_session(
                session, working_dir=directory, command=[sys.executable, "-u", "-c", program]
            ):
                raise RuntimeError("could not start the smoke session")
            await wait_for_output(session, "smoke-ready")
            fields = await query_format_vars(session, "session_name=#{session_name}")
            if fields.get("session_name") != session:
                raise RuntimeError(f"display-message targeted the wrong session: {fields!r}")
            if not await paste_message(session, marker):
                raise RuntimeError("paste_message failed")
            if not await send_keys(session, "Enter"):
                raise RuntimeError("send_keys failed")
            await wait_for_output(session, f"received:{marker}")
        finally:
            if not await stop_session(session):
                raise RuntimeError(f"could not remove smoke session {session}")
    print("tmux smoke passed: paste, keys, capture, display and cleanup")


if __name__ == "__main__":
    asyncio.run(main())
