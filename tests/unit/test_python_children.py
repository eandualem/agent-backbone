"""Execute owned child interpreters without tmux, native CLIs or model calls."""

import json
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_backbone.hooks import install as hooks
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.terminal import _sessions


@pytest.fixture
def child_python(tmp_path):
    """An uncached site import detects writes even for a Python -I subprocess."""
    prefix = tmp_path / "python"
    venv.EnvBuilder(with_pip=False).create(prefix)
    python = prefix / "bin/python"
    site = next(prefix.glob("lib/python*/site-packages"))
    trace = tmp_path / "flags"
    (site / "cache_probe.py").write_text(
        "import sys\n"
        f"with open({str(trace)!r}, 'a') as out:\n"
        "    out.write(str(sys.flags.dont_write_bytecode) + '\\n')\n"
    )
    (site / "cache_probe.pth").write_text("import cache_probe\n")
    # Prove the fixture detects the original failure; no inherited -B setting.
    subprocess.run([str(python), "-I", "-c", "pass"], env={}, check=True)
    caches = list(prefix.rglob("*.pyc"))
    assert caches and "0" in trace.read_text().splitlines()
    for cache in caches:
        cache.unlink()
    trace.unlink()
    yield str(python)
    assert trace.read_text().splitlines()
    assert set(trace.read_text().splitlines()) == {"1"}
    assert not list(prefix.rglob("*.pyc"))


@pytest.mark.parametrize(
    "runtime_id", ["claude", "codex", "gemini", "opencode", "deepcode", "aider", "shell"]
)
async def test_runtime_launch_children_leave_interpreter_uncached(
    runtime_id, child_python, tmp_path, monkeypatch
):
    runtime = RUNTIMES[runtime_id]
    monkeypatch.setattr(hooks, "default_python", lambda: child_python)
    monkeypatch.setattr(type(runtime), "installed_version", lambda self: "2.1.4")
    monkeypatch.setattr(
        "agent_backbone.services.runtimes.base.resolve_command", lambda name: "/usr/bin/true"
    )
    brief = tmp_path / "brief"
    brief.write_text("Test instructions")
    command = runtime.build_command(
        data_dir=tmp_path / "data", state_dir=tmp_path / "state", brief_file=brief
    ) or ["/usr/bin/true"]
    monkeypatch.setattr(_sessions, "sys", SimpleNamespace(executable=child_python))
    monkeypatch.setattr(_sessions, "session_exists", AsyncMock(return_value=False))
    # tmux quoting is exercised by the smoke suite. Here capture the actual
    # generated child argv and execute it directly, with native CLIs replaced.
    monkeypatch.setattr(_sessions, "_tmux_quote", shlex.quote)

    async def run_tmux(*args, **kwargs):
        words = shlex.split(kwargs["stdin_data"].decode())
        child = words[words.index("env") + 1 :]
        result = subprocess.run(child, env={}, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
        return result.returncode, result.stdout, result.stderr

    monkeypatch.setattr(_sessions, "_run_tmux", run_tmux)
    assert await _sessions.start_session("test", command=command, environment={"FIXTURE": "value"})


@pytest.mark.parametrize("runtime_id", ["claude", "codex", "gemini"])
def test_python_hook_children_leave_interpreter_uncached(runtime_id, child_python, tmp_path):
    command, _ = RUNTIMES[runtime_id].hook_settings(
        tmp_path / "data", tmp_path / "state", python=child_python
    )
    subprocess.run(shlex.split(command), input=b"{}", env={}, capture_output=True, check=True)


@pytest.mark.parametrize("isolated", [False, True])
async def test_detached_daemon_retains_import_mode_without_caches(isolated, monkeypatch, tmp_path):
    from agent_backbone.cli import server
    from agent_backbone.config import bootstrap_config
    from agent_backbone.services import terminal

    monkeypatch.setattr(
        server,
        "sys",
        SimpleNamespace(executable=sys.executable, flags=SimpleNamespace(isolated=isolated)),
    )
    monkeypatch.setattr(terminal, "session_exists", AsyncMock(return_value=False))
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(terminal, "start_session", start)
    monkeypatch.setattr("agent_backbone.cli.service.state", lambda: "installed")
    assert await server._up_detached(bootstrap_config(tmp_path)) == 0
    command = start.call_args.kwargs["command"]
    # Observe the actual interpreter flags, stopping before the daemon entrypoint.
    probe = [
        *command[: command.index("-m")],
        "-c",
        "import sys,json; print(json.dumps([sys.flags.isolated, sys.flags.dont_write_bytecode]))",
    ]
    result = subprocess.run(probe, env={}, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [isolated, True]


@pytest.mark.parametrize("isolated", [False, True])
def test_version_probe_preserves_import_mode(isolated, monkeypatch):
    from unittest.mock import Mock

    from agent_backbone.cli import upgrade

    monkeypatch.setattr(
        upgrade,
        "sys",
        SimpleNamespace(executable=sys.executable, flags=SimpleNamespace(isolated=isolated)),
    )
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="0.4.0\n"))
    monkeypatch.setattr(upgrade.subprocess, "run", run)
    assert upgrade._fresh_version() == "0.4.0"
    assert run.call_args.args[0][:-1] == [sys.executable, *(["-I"] if isolated else []), "-B", "-c"]


def test_generated_chrome_host_leaves_interpreter_uncached(child_python, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "chrome_host_manifest", lambda: tmp_path / "manifest.json")
    data = tmp_path / "data"
    hooks.install_chrome_tab_names(data, tmp_path / "state", python=child_python)
    subprocess.run([str(data / "chrome-tab-names/host")], input=b"", env={}, check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the shipped plugin")
def test_opencode_shell_parser_leaves_interpreter_uncached(child_python, tmp_path):
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hooks.hook_source("opencode_hook.js").read_text())
    shutil.copyfile(hooks.hook_source("backbone_state.py"), tmp_path / "backbone_state.py")
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const hook = await AgentBackbone();
await hook["tool.execute.before"]({tool: "bash"}, {args: {command: "echo fixture"}});
"""
    subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script, plugin.as_uri()],
        env={
            "PATH": str(Path(child_python).parent),
            "BACKBONE_AGENT": "fixture",
            "BACKBONE_STATE_DIR": str(tmp_path / "state"),
        },
        capture_output=True,
        check=True,
        timeout=20,
    )
