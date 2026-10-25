from unittest.mock import patch

from agent_backbone.services.terminal import foreground_commands


async def test_only_terminal_foreground_process_group_is_returned():
    with (
        patch(
            "agent_backbone.services.terminal._foreground.query_format_vars",
            return_value={"tty": "/dev/pts/3"},
        ),
        patch(
            "agent_backbone.services.terminal._foreground._output",
            return_value=(
                0,
                "10 20 /bin/zsh\n20 20 /usr/bin/node /opt/cli/index.js\n"
                "30 20 node /background.js\n",
            ),
        ) as ps,
    ):
        assert await foreground_commands("worker") == [["/usr/bin/node", "/opt/cli/index.js"]]
    assert ps.call_args.args == ("ps", "-ww", "-t", "pts/3", "-o", "pgid=,tpgid=,args=")
