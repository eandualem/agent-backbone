"""Manual real-tmux check: ``make smoke``. No backbone service or model required."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from agent_backbone.services.runtimes import send_message
from agent_backbone.services.terminal import (
    capture_pane,
    foreground_commands,
    paste_message,
    pin_tmux_server,
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


async def main(*, pinned: bool = False, isolated: bool = False) -> None:
    pinned = pinned or isolated
    session = f"backbone-smoke-{uuid.uuid4().hex}"
    marker = uuid.uuid4().hex
    # Characters tmux's command syntax treats specially, handed over on stdin.
    value = f"$HOME ~ 'q' \"d\" \\ {marker[:8]}"
    program = (
        "import hashlib, os, sys\n"
        "print('smoke-ready:' + os.environ['BACKBONE_SMOKE'] + os.environ['SHELL'], flush=True)\n"
        "print('smoke-cwd:' + hashlib.sha256(os.getcwdb()).hexdigest(), flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('received:' + line.strip(), flush=True)\n"
    )
    # Under /tmp, tmux's own default: the socket path stays within macOS's 104 bytes.
    with tempfile.TemporaryDirectory(prefix="backbone-smoke-", dir="/tmp") as directory:
        # A private server, never the one the caller's own session runs in (#351).
        for key in list(os.environ):
            if key.startswith("BACKBONE_") or key in ("TMUX", "TMUX_PANE"):
                os.environ.pop(key, None)
        home = Path(directory) / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        working = Path(directory) / "literal # #[fg=red] #{HOME} #(printf smoke)"
        working.mkdir()
        os.environ["TMUX_TMPDIR"] = directory
        tempfile.tempdir = directory  # where start_session writes the environment file
        socket = Path(directory) / f"tmux-{os.getuid()}" / "default"
        decoy = Path(directory) / "other.sock"
        if pinned:
            socket = Path(directory) / "selected socket.sock"
            pin_tmux_server(socket)
        if isolated:
            pane_program = (
                f"assert os.environ['TMUX'].rsplit(',', 2)[0] == {str(socket.resolve())!r}\n"
                "assert os.environ['TMUX_PANE'].startswith('%')\n"
                "assert os.environ.get('TERM')\n"
                "assert 'BACKBONE_HOST_SENTINEL' not in os.environ\n"
                "print('smoke-pane:verified', flush=True)\n"
            )
            program = "import os\n" + pane_program + program
            os.environ["BACKBONE_HOST_SENTINEL"] = "host-only"
        try:
            if pinned:
                subprocess.run(
                    [
                        "tmux",
                        "-S",
                        str(decoy),
                        "new-session",
                        "-d",
                        "-s",
                        session,
                        "printf decoy-ready; exec cat",
                    ],
                    check=True,
                )
                # Even a valid inherited server with an identically named session
                # must not redirect commands away from the pinned provider.
                os.environ["TMUX"] = f"{decoy},0,0"
                os.environ["TMUX_TMPDIR"] = str(Path(directory) / "wrong")
            if not await start_session(
                session,
                working_dir=str(working),
                command=[sys.executable, "-u", "-c", program],
                # tmux sets SHELL in the pane; the agent's own value must win.
                environment={
                    "BACKBONE_SMOKE": value,
                    "SHELL": "/smoke/sh",
                    **({"HOME": str(home), "TMPDIR": directory} if isolated else {}),
                },
                inherit_environment=not isolated,
            ):
                raise RuntimeError("could not start the smoke session")
            await wait_for_output(session, f"smoke-ready:{value}/smoke/sh")
            if isolated:
                await wait_for_output(session, "smoke-pane:verified")
            await wait_for_output(
                session, "smoke-cwd:" + hashlib.sha256(os.fsencode(working.resolve())).hexdigest()
            )
            fields = await query_format_vars(
                session,
                "session_name=#{session_name}\nsocket_path=#{socket_path}\n"
                "start=#{pane_start_command}",
            )
            if marker[:8] in fields.get("start", ""):
                raise RuntimeError(f"an environment value is in the start command: {fields!r}")
            if leftover := list(Path(directory).glob("backbone-env-*")):
                raise RuntimeError(f"the environment file outlived its reading: {leftover!r}")
            if fields.get("session_name") != session:
                raise RuntimeError(f"display-message targeted the wrong session: {fields!r}")
            server = Path(fields.get("socket_path", "")).resolve()
            if not server.is_relative_to(Path(directory).resolve()):
                raise RuntimeError(f"refusing to run on a server outside {directory}: {fields!r}")
            if pinned and server != socket.resolve():
                raise RuntimeError("the pinned socket was not used")
            foreground = await foreground_commands(session)
            if not any(
                command and "python" in Path(command[0]).name.lower() for command in foreground
            ):
                raise RuntimeError(f"foreground process was not identified: {foreground!r}")
            # Neither a shell adapter nor a stale runtime hint may submit to
            # this non-model foreground process.
            for hint in ("shell", "codex", None):
                if (
                    await send_message(session, "not-a-model-notification", runtime_hint=hint)
                    is not None
                ):
                    raise RuntimeError("an unidentified receiver accepted a notification")
            if not await paste_message(session, marker):
                raise RuntimeError("paste_message failed")
            if not await send_keys(session, "Enter"):
                raise RuntimeError("send_keys failed")
            await wait_for_output(session, f"received:{marker}")
            if pinned:
                output = subprocess.check_output(
                    ["tmux", "-S", str(decoy), "capture-pane", "-p", "-t", f"={session}:"]
                ).decode()
                if "decoy-ready" not in output or marker in output:
                    raise RuntimeError("the other server was changed")
        finally:
            removed = await stop_session(session)
            # A user configuration with ``exit-empty off`` keeps an empty server alive.
            subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
            if pinned:
                other_alive = (
                    subprocess.run(
                        ["tmux", "-S", str(decoy), "has-session", "-t", f"={session}:"],
                        capture_output=True,
                    ).returncode
                    == 0
                )
                subprocess.run(["tmux", "-S", str(decoy), "kill-server"], capture_output=True)
                if not other_alive:
                    raise RuntimeError("stopping the selected session affected the other server")
            if not removed:
                raise RuntimeError(f"could not remove smoke session {session}")
    print("tmux smoke passed: environment, paste, keys, capture, display and cleanup")


if __name__ == "__main__":
    asyncio.run(main(pinned="--pinned" in sys.argv, isolated="--isolated" in sys.argv))
