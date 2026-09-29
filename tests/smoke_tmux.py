"""Manual real-tmux check: ``make smoke``. No backbone service or model required."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

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
    # Under /tmp, tmux's own default: the socket path stays within macOS's 104 bytes.
    with tempfile.TemporaryDirectory(prefix="backbone-smoke-", dir="/tmp") as directory:
        # A private server, never the one the caller's own session runs in (#351).
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        os.environ["TMUX_TMPDIR"] = directory
        socket = ""
        try:
            if not await start_session(
                session, working_dir=directory, command=[sys.executable, "-u", "-c", program]
            ):
                raise RuntimeError("could not start the smoke session")
            await wait_for_output(session, "smoke-ready")
            fields = await query_format_vars(
                session, "session_name=#{session_name}\nsocket_path=#{socket_path}"
            )
            if fields.get("session_name") != session:
                raise RuntimeError(f"display-message targeted the wrong session: {fields!r}")
            server = Path(fields.get("socket_path", "")).resolve()
            if not server.is_relative_to(Path(directory).resolve()):
                raise RuntimeError(f"refusing to run on a server outside {directory}: {fields!r}")
            socket = str(server)
            if not await paste_message(session, marker):
                raise RuntimeError("paste_message failed")
            if not await send_keys(session, "Enter"):
                raise RuntimeError("send_keys failed")
            await wait_for_output(session, f"received:{marker}")
        finally:
            removed = await stop_session(session)
            if socket:
                # A user configuration with ``exit-empty off`` keeps an empty server alive.
                subprocess.run(["tmux", "-S", socket, "kill-server"], capture_output=True)
            if not removed:
                raise RuntimeError(f"could not remove smoke session {session}")
    print("tmux smoke passed: paste, keys, capture, display and cleanup")


if __name__ == "__main__":
    asyncio.run(main())
