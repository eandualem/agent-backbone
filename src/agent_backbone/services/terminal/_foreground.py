"""Read process arguments for a pane's foreground process group."""

import shlex

from agent_backbone.services.terminal._callers import CallerUnknown, _output
from agent_backbone.services.terminal._sessions import query_format_vars


async def foreground_commands(session_name: str) -> list[list[str]]:
    pane = await query_format_vars(session_name, "tty=#{pane_tty}")
    tty = pane.get("tty", "")
    if not tty.startswith("/dev/"):
        return []
    try:
        code, output = await _output("ps", "-ww", "-t", tty[5:], "-o", "pgid=,tpgid=,args=")
    except CallerUnknown:
        return []
    if code:
        return []
    commands = []
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or parts[0] != parts[1]:
            continue
        try:
            commands.append(shlex.split(parts[2]))
        except ValueError:
            continue
    return commands
