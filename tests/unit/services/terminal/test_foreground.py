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
                "10 10 20\n20 20 20\n30 30 20\n",
            ),
        ) as ps,
        patch(
            "agent_backbone.services.terminal._foreground._arguments",
            return_value=["node", "/opt/with space/cli.js"],
        ) as arguments,
    ):
        assert await foreground_commands("worker") == [["node", "/opt/with space/cli.js"]]
    assert ps.call_args.args == ("ps", "-t", "pts/3", "-o", "pid=,pgid=,tpgid=")
    arguments.assert_called_once_with(20)


def test_kernel_arguments_preserve_spaces_and_apostrophes(tmp_path):
    import subprocess
    import sys

    from agent_backbone.services.terminal._foreground import _arguments

    script = tmp_path / "CLI with spaces and 'quote'.py"
    script.write_text("print('ready', flush=True)\ninput()\n")
    child = subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        assert child.stdout.readline() == b"ready\n"
        assert _arguments(child.pid)[1:] == [str(script)]
    finally:
        child.terminate()
        child.wait(timeout=5)
