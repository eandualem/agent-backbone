"""Read arguments for a pane's foreground processes without shell parsing."""

import ctypes
import sys
from pathlib import Path

from agent_backbone.services.terminal._callers import CallerUnknown, _output
from agent_backbone.services.terminal._sessions import query_format_vars


def _arguments(pid: int) -> list[str]:
    try:
        if sys.platform == "darwin":
            # KERN_PROCARGS2: argc, executable path, padding, then NUL-separated argv.
            mib = (ctypes.c_int * 3)(1, 49, pid)
            buffer = ctypes.create_string_buffer(1024 * 1024)
            size = ctypes.c_size_t(len(buffer))
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
                return []
            raw = buffer.raw[: size.value]
            count = int.from_bytes(raw[:4], sys.byteorder)
            _, separator, arguments = raw[4:].partition(b"\0")
            if not separator or not 0 < count < 65536:
                return []
            values = arguments.lstrip(b"\0").split(b"\0")[:count]
        elif sys.platform.startswith("linux"):
            values = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        else:
            return []
        return [value.decode(errors="surrogateescape") for value in values]
    except (OSError, ValueError):
        return []


async def foreground_commands(session_name: str) -> list[list[str]]:
    """Arguments of the pane's foreground process group leader. Its children are
    excluded: a launch helper's private server is not the interactive receiver."""
    pane = await query_format_vars(session_name, "tty=#{pane_tty}")
    tty = pane.get("tty", "")
    if not tty.startswith("/dev/"):
        return []
    try:
        code, output = await _output("ps", "-t", tty[5:], "-o", "pid=,pgid=,tpgid=")
    except CallerUnknown:
        return []
    if code:
        return []
    commands = []
    for line in output.splitlines():
        parts = line.split()
        if (
            len(parts) == 3
            and all(part.isdigit() for part in parts)
            and parts[0] == parts[1] == parts[2]
            and (arguments := _arguments(int(parts[0])))
        ):
            commands.append(arguments)
    return commands
