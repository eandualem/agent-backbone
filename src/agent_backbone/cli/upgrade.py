"""``backbone upgrade`` — new code in, one restart, agents untouched."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys

from agent_backbone.cli import _common
from agent_backbone.config import BackboneConfig
from agent_backbone.release import installation, installed_version, latest_published


def _fresh_version() -> str:
    """The version a new process would report (this one may predate the upgrade)."""
    probe = "from agent_backbone.release import installed_version as v; print(v())"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"


async def _wait_for_api(config: BackboneConfig, seconds: float = 30.0) -> str | None:
    """The running version once the API answers, or None after ``seconds``."""
    for _ in range(int(seconds * 2)):
        health = await _common.api(config, "GET", "/health", timeout=2.0)
        if health is not None and isinstance(health[1], dict):
            return str(health[1].get("version") or "?")
        await asyncio.sleep(0.5)
    return None


async def restart_backbone(config: BackboneConfig) -> int:
    """Restart whatever runs the backbone: the login service, else the tmux session."""
    from agent_backbone.cli import server, service
    from agent_backbone.services.terminal import session_exists

    if service.state() == "running":
        rc = service.restart()
        if rc:
            return rc
    elif await session_exists(config.backbone.session_name):
        await server._down(config)
        rc = await server._up_detached(config)
        if rc:
            return rc
    elif await _common.api_up(config):
        print("the backbone is running outside the login service and tmux; restart it yourself")
        return 1
    else:
        print("the backbone is not running; nothing to restart")
        print("start it with `backbone service install` (at login) or `backbone up --detach`")
        return 0
    version = await _wait_for_api(config)
    if version is None:
        print("restarted, but the API did not answer within 30s")
        return 1
    print(f"backbone is back (version {version})")
    return 0


async def _upgrade(args: argparse.Namespace) -> int:
    install = installation()
    before = installed_version()
    print(f"install: {install.describe()}")
    print(f"installed: {before}")
    if args.check:
        latest = latest_published()
        print(f"latest on PyPI: {latest or 'unreachable'}")
        if install.kind == "editable":
            print("a development checkout runs what is checked out: pull, then `backbone upgrade`")
        elif latest and latest != before:
            print("run `backbone upgrade` to install it and restart")
        return 0

    command = install.upgrade_command
    if command:
        print("$ " + " ".join(command))
        rc = subprocess.run(command, check=False).returncode
        if rc != 0:
            print("upgrade failed; nothing was restarted")
            return 1
        after = _fresh_version()
        print(f"installed: {after}" if after != before else f"already at {before}")
    elif install.kind == "editable":
        print("a development checkout runs whatever is checked out; nothing to download")
    else:
        print("installed some other way: upgrade it yourself (pip install -U agent-backbone)")

    if args.no_restart:
        print("not restarting (--no-restart); the running backbone keeps the old code")
        return 0
    config = await _common.load_config()
    return await restart_backbone(config)


def cmd_upgrade(args: argparse.Namespace) -> int:
    return asyncio.run(_upgrade(args))
