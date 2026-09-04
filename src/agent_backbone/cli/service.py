"""``backbone service install|uninstall|restart|status`` — start the backbone at login.

Agents are tmux sessions and end with a reboot, but the backbone itself is
a plain server process: on macOS a LaunchAgent (``launchd``) and on Linux
a user unit (``systemd --user``) start it at login and restart it if it
dies. The service runs ``backbone up`` in the foreground with the same
data directory this command was run with; ``backbone up --detach`` (a
tmux session) stays the manual alternative.
"""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from agent_backbone.config import bootstrap_config

LABEL = "dev.agent-backbone"


def _backbone_binary() -> str:
    """The ``backbone`` executable to launch: the one on PATH, else this interpreter's."""
    found = shutil.which("backbone")
    if found:
        return found
    sibling = Path(sys.executable).with_name("backbone")
    return str(sibling) if sibling.exists() else "backbone"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "agent-backbone.service"


def _plist(binary: str, data_dir: Path, log: Path) -> str:
    path_var = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    binary, data_dir, log, path_var = (escape(str(v)) for v in (binary, data_dir, log, path_var))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{binary}</string><string>up</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>BACKBONE_DATA_DIR</key><string>{data_dir}</string>
    <key>PATH</key><string>{path_var}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def _unit(binary: str, data_dir: Path) -> str:
    path_var = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    # systemd splits ExecStart on whitespace and reads Environment= as
    # space-separated assignments: quote so paths with spaces survive.
    return f"""[Unit]
Description=agent-backbone — local control plane for terminal AI agents
After=default.target

[Service]
ExecStart={shlex.quote(binary)} up
Environment={shlex.quote(f"BACKBONE_DATA_DIR={data_dir}")}
Environment={shlex.quote(f"PATH={path_var}")}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


_NOT_FOUND = 127  # the shell's own code for a missing command


def _run(*args: str) -> subprocess.CompletedProcess:
    """Run the service manager; a missing binary (a container, a minimal
    image) is an ordinary failure, not a traceback."""
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, _NOT_FOUND, "", f"{args[0]}: not found")


def _no_manager(system: str) -> str:
    manager = "launchd" if system == "Darwin" else "systemd --user"
    return f"no {manager} on this machine; use `backbone up --detach` (and again after a reboot)"


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def install() -> int:
    config = bootstrap_config()
    binary = _backbone_binary()
    system = platform.system()
    if system == "Darwin":
        plist = _plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(_plist(binary, config.data_dir, config.data_dir / "backbone.log"))
        _run("launchctl", "bootout", _gui_domain(), str(plist))  # replace an older copy
        result = _run("launchctl", "bootstrap", _gui_domain(), str(plist))
        if result.returncode != 0:
            plist.unlink()  # a failed install leaves nothing behind
            if result.returncode == _NOT_FOUND:
                print(_no_manager(system))
            else:
                detail = result.stderr.strip() or result.stdout.strip()
                print(f"launchctl bootstrap failed: {detail}")
            return 1
        print(f"installed {plist}")
        print("the backbone now starts at login and restarts if it dies")
        print(f"log: {config.data_dir / 'backbone.log'}")
        return 0
    if system == "Linux":
        unit = _unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(_unit(binary, config.data_dir))
        for step in (("daemon-reload",), ("enable", "--now", "agent-backbone.service")):
            result = _run("systemctl", "--user", *step)
            if result.returncode != 0:
                unit.unlink()  # a failed install leaves nothing behind
                if result.returncode == _NOT_FOUND:
                    print(_no_manager(system))
                else:
                    print(f"systemctl --user {' '.join(step)} failed: {result.stderr.strip()}")
                return 1
        print(f"installed {unit}")
        print("the backbone now starts at login and restarts if it dies")
        print("logs: journalctl --user -u agent-backbone")
        return 0
    print(f"no service manager support for {system}; use `backbone up --detach` after a reboot")
    return 1


def uninstall() -> int:
    system = platform.system()
    if system == "Darwin":
        plist = _plist_path()
        if not plist.exists():
            print("not installed")
            return 0
        _run("launchctl", "bootout", _gui_domain(), str(plist))
        plist.unlink()
        print(f"removed {plist}")
        return 0
    if system == "Linux":
        unit = _unit_path()
        if not unit.exists():
            print("not installed")
            return 0
        _run("systemctl", "--user", "disable", "--now", "agent-backbone.service")
        unit.unlink()
        _run("systemctl", "--user", "daemon-reload")
        print(f"removed {unit}")
        return 0
    print(f"no service manager support for {system}")
    return 1


def restart() -> int:
    """Restart the login service (the building block of ``backbone upgrade``)."""
    system = platform.system()
    if system == "Darwin":
        if not _plist_path().exists():
            print("not installed")
            return 1
        result = _run("launchctl", "kickstart", "-k", f"{_gui_domain()}/{LABEL}")
    elif system == "Linux":
        if not _unit_path().exists():
            print("not installed")
            return 1
        result = _run("systemctl", "--user", "restart", "agent-backbone.service")
    else:
        print(f"no service manager support for {system}")
        return 1
    if result.returncode == _NOT_FOUND:
        print(_no_manager(system))
        return 1
    if result.returncode != 0:
        print(f"restart failed: {result.stderr.strip() or result.stdout.strip()}")
        return 1
    print("service restarted")
    return 0


def state() -> str:
    """``running``, ``installed`` (present, not running), ``not installed`` or ``unsupported``."""
    system = platform.system()
    if system == "Darwin":
        result = _run("launchctl", "print", f"{_gui_domain()}/{LABEL}")
        if result.returncode == _NOT_FOUND:
            return "unsupported"
        if not _plist_path().exists():
            return "not installed"
        return (
            "running"
            if result.returncode == 0 and "state = running" in result.stdout
            else "installed"
        )
    if system == "Linux":
        result = _run("systemctl", "--user", "is-active", "agent-backbone.service")
        if result.returncode == _NOT_FOUND:
            return "unsupported"
        if not _unit_path().exists():
            return "not installed"
        return "running" if result.stdout.strip() == "active" else "installed"
    return "unsupported"


def cmd_service(args: argparse.Namespace) -> int:
    sub = args.service_command
    if sub == "install":
        return install()
    if sub == "uninstall":
        return uninstall()
    if sub == "restart":
        return restart()
    print(f"service: {state()}")
    return 0
