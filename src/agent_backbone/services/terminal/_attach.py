"""Interactive attachment for the local CLI, without replacing an agent session."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent_backbone.services.terminal._core import exact_target
from agent_backbone.services.terminal._server import tmux_command, tmux_server_socket


def attach_session(name: str, *, read_only: bool = False) -> int:
    """Use the caller's terminal; switch clients when already inside tmux.

    Read-only attachment needs a separate client and must never change the
    permissions of the caller's existing tmux client.
    """
    if inherited := os.environ.get("TMUX"):
        if read_only:
            raise ValueError("open another terminal for --read-only attachment")
        selected = tmux_server_socket()
        if selected and str(Path(inherited.rsplit(",", 2)[0]).resolve()) != selected:
            raise ValueError("open another terminal to attach to a different tmux server")
        command = tmux_command("switch-client", "-t", exact_target(name))
    else:
        command = tmux_command("attach-session", "-t", exact_target(name))
        if read_only:
            command.append("-r")
    return subprocess.call(command)
