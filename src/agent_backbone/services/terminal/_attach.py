"""Interactive attachment for the local CLI, without replacing an agent session."""

from __future__ import annotations

import os
import subprocess

from agent_backbone.services.terminal._core import exact_target


def attach_session(name: str, *, read_only: bool = False) -> int:
    """Use the caller's terminal; switch clients when already inside tmux.

    Read-only attachment needs a separate client and must never change the
    permissions of the caller's existing tmux client.
    """
    if os.environ.get("TMUX"):
        if read_only:
            raise ValueError("open another terminal for --read-only attachment")
        command = ["tmux", "switch-client", "-t", exact_target(name)]
    else:
        command = ["tmux", "attach-session", "-t", exact_target(name)]
        if read_only:
            command.append("-r")
    return subprocess.call(command)
