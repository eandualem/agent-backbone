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
import json
import os
import platform
import plistlib
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


def _plist(binary: str, data_dir: Path, log: Path, *, search_path: str | None = None) -> str:
    # ProcessType Interactive: the tmux server and every agent the service
    # starts inherit its scheduling, and launchd's default for a LaunchAgent
    # throttles them below ordinary apps (priority 20 instead of 31, measured).
    # The backbone writes and rotates `log` itself; launchd's own file keeps
    # only what is printed outside logging (a crash before logging starts).
    path_var = (
        search_path
        if search_path is not None
        else os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    )
    output = log.with_suffix(".out.log")
    binary, data_dir, log, output, path_var = (
        escape(str(v)) for v in (binary, data_dir, log, output, path_var)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{binary}</string><string>up</string>
    <string>--log-file</string><string>{log}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>BACKBONE_DATA_DIR</key><string>{data_dir}</string>
    <key>PATH</key><string>{path_var}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{output}</string>
  <key>StandardErrorPath</key><string>{output}</string>
</dict></plist>
"""


def _unit_quote(value: str) -> str:
    # systemd.syntax requires whole-item quotes and C-style backslash escapes;
    # shell quote concatenation (used for apostrophes by shlex) is invalid here.
    if any(c in "'\\" or ord(c) < 32 for c in value):
        return json.dumps(value, ensure_ascii=False)
    return shlex.quote(value)


def _unit(binary: str, data_dir: Path, *, search_path: str | None = None) -> str:
    path_var = (
        search_path
        if search_path is not None
        else os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    )
    binary = binary.replace("%", "%%").replace("$", "$$")
    directory = str(data_dir).replace("%", "%%")
    path_var = path_var.replace("%", "%%")
    # systemd splits ExecStart on whitespace and reads Environment= as
    # space-separated assignments: quote so paths with spaces survive.
    return f"""[Unit]
Description=agent-backbone — local control plane for terminal AI agents
After=default.target

[Service]
ExecStart={_unit_quote(binary)} up
Environment={_unit_quote(f"BACKBONE_DATA_DIR={directory}")}
Environment={_unit_quote(f"PATH={path_var}")}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def setup_binary() -> Path:
    """Pin a fresh service to this installation, never a different CLI on PATH."""
    prefix = Path(sys.prefix).resolve()
    # The portable engine has bin/backbone beside its private python/ prefix.
    candidates = [prefix / "bin/backbone"]
    manifest = prefix.parent / "engine-manifest.json"
    if prefix.name == "python" and manifest.is_file():
        payload = json.loads(manifest.read_text())
        if payload.get("schema_version") != 1 or payload.get("entrypoint") != "bin/backbone":
            raise ValueError("Invalid portable engine manifest.")
        candidates = [prefix.parent / "bin/backbone"]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve()
            if not resolved.is_relative_to(
                prefix.parent if manifest.is_file() and prefix.name == "python" else prefix
            ):
                raise ValueError("The launcher redirects outside this installation.")
            return resolved
    raise ValueError("This installation has no executable Backbone launcher.")


def setup_service_path() -> Path | None:
    system = platform.system()
    return _plist_path() if system == "Darwin" else _unit_path() if system == "Linux" else None


def setup_service_definition(binary: Path, data_dir: Path) -> tuple[Path, str]:
    path = setup_service_path()
    if path is None:
        raise ValueError("No supported user service manager is available.")
    search_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if len(search_path) > 16_384 or any(ord(c) < 32 or ord(c) == 127 for c in search_path):
        raise ValueError("The executable search path is invalid.")
    # A daemon has no meaningful invoking working directory; omit relative entries.
    entries = [entry for entry in search_path.split(os.pathsep) if Path(entry).is_absolute()]
    fallback = binary.parent.parent / "libexec/tmux"
    if (
        fallback.is_file()
        and os.access(fallback, os.X_OK)
        and fallback.resolve().is_relative_to(binary.parent.parent.resolve())
    ):
        entries.append(str(fallback.parent))
    search_path = os.pathsep.join(dict.fromkeys([str(binary.parent), *entries]))
    content = (
        _plist(str(binary), data_dir, data_dir / "backbone.log", search_path=search_path)
        if platform.system() == "Darwin"
        else _unit(str(binary), data_dir, search_path=search_path)
    )
    return path, content


def setup_service_owner() -> Path | None:
    """Read the existing service's explicit data dir without installing or repairing it."""
    path = setup_service_path()
    if path is None or not path.is_file():
        return None
    try:
        if platform.system() == "Darwin":
            value = plistlib.loads(path.read_bytes())["EnvironmentVariables"]["BACKBONE_DATA_DIR"]
        else:
            values = [
                item.split("=", 1)[1].replace("%%", "%")
                for line in path.read_text().splitlines()
                if line.startswith("Environment=")
                for item in shlex.split(line.partition("=")[2])
                if item.startswith("BACKBONE_DATA_DIR=")
            ]
            if len(values) != 1:
                return None
            value = values[0]
        return Path(value).resolve() if Path(value).is_absolute() else None
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
        return None


def setup_service_state() -> str:
    """Bounded observation for the installer's finite action."""
    path = setup_service_path()
    if path is None:
        return "unsupported"
    if not path.exists():
        return "not_installed"
    mac = platform.system() == "Darwin"
    command = (
        ("launchctl", "print", f"{_gui_domain()}/{LABEL}")
        if mac
        else ("systemctl", "--user", "is-active", "agent-backbone.service")
    )
    try:
        response = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
    except FileNotFoundError:
        return "unsupported"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    running = "state = running" in response.stdout if mac else response.stdout.strip() == "active"
    return "running" if response.returncode == 0 and running else "installed"


def start_setup_service() -> bool:
    """Start a newly owned definition. Never boot out or replace a service."""
    path = setup_service_path()
    if path is None:
        return False
    if setup_service_state() == "running":
        return True
    commands = (
        [("launchctl", "bootstrap", _gui_domain(), str(path))]
        if platform.system() == "Darwin"
        else [
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "agent-backbone.service"),
        ]
    )
    try:
        return all(
            subprocess.run(command, capture_output=True, timeout=15, check=False).returncode == 0
            for command in commands
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


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
