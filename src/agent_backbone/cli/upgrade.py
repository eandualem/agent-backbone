"""``backbone upgrade`` — new code in, one restart, agents untouched."""

from __future__ import annotations

import argparse
import asyncio
import math
import subprocess
import sys
from uuid import uuid4

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


async def _generation(config: BackboneConfig) -> tuple[float, str] | None:
    """``(started, version)`` of the process answering /health, or None when none does.

    An answer without a parseable ``started`` (a proxy error page, a stale
    cache) is not a generation — treating it as one would mistake it for
    the new build.
    """
    health = await _common.api(config, "GET", "/health", timeout=2.0)
    if health is None or health[0] != 200 or not isinstance(health[1], dict):
        return None
    try:
        started = float(health[1]["started"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(started) or started <= 0:
        # NaN never equals the previous generation, so it would always look
        # like a new build; a non-positive start time is no generation.
        return None
    return (started, str(health[1].get("version") or "?"))


async def _wait_for_api(
    config: BackboneConfig, seconds: float = 30.0, *, before: float | None = None
) -> str | None:
    """The version of the *new* process once it answers, or None after ``seconds``.

    A service manager returns from its restart while the old process may
    still be serving; the process start time in /health tells them apart.
    """
    for _ in range(int(seconds * 2)):
        generation = await _generation(config)
        if generation is not None:
            started, version = generation
            if before is None or started is None or started != before:
                return version
        await asyncio.sleep(0.5)
    return None


async def restart_backbone(config: BackboneConfig) -> int:
    """Restart whatever runs the backbone: the login service, else the tmux session."""
    from agent_backbone.cli import server, service
    from agent_backbone.services.terminal import session_exists

    old = await _generation(config)
    before = old[0] if old else None
    if service.state() == "running":
        rc = service.restart()
        if rc:
            return rc
    elif await session_exists(config.backbone.session_name):
        down_rc = await server._down(config)
        if down_rc:
            return down_rc
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
    version = await _wait_for_api(config, before=before)
    if version is None:
        print("restarted, but a new backbone did not answer within 30s")
        return 1
    print(f"backbone is back (version {version})")
    return 0


async def _set_restart_hold(config: BackboneConfig, operation: str, enabled: bool) -> bool:
    result = await _common.api(
        config,
        "POST",
        "/api/upgrade/hold",
        json_body={"operation_id": operation, "enabled": enabled},
    )
    return bool(
        result
        and result[0] == 200
        and isinstance(result[1], dict)
        and result[1].get("operation_held") is enabled
    )


async def _upgrade(args: argparse.Namespace) -> int:
    if args.check or not args.no_restart:
        return await _install_upgrade(args)
    # Coordinate before the installer can change any files. Refuse an old or
    # unhealthy API that cannot acknowledge this hold instead of making a promise
    # that its watcher will immediately break.
    config = await _common.read_client_config()
    health = await _common.api(config, "GET", "/health", timeout=2.0)
    operation = str(uuid4()) if health is not None else None
    if operation and (health[0] != 200 or not await _set_restart_hold(config, operation, True)):
        print(
            "cannot hold automatic restart; restart/update the service "
            "before upgrading with --no-restart"
        )
        return 1
    # A failed installer may have changed files too: --no-restart keeps the
    # hold on failure as well. A later explicit service restart clears it.
    return await _install_upgrade(args)


async def _install_upgrade(args: argparse.Namespace) -> int:
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
        print("installed some other way: upgrade it yourself (pip install -U agent-backbone),")
        print("then `backbone service restart`; nothing was restarted")
        return 1

    if args.no_restart:
        print(
            "not restarting (--no-restart); automatic restart is held "
            "until a manual service restart"
        )
        return 0
    config = await _common.load_config()
    return await restart_backbone(config)


def cmd_upgrade(args: argparse.Namespace) -> int:
    return asyncio.run(_upgrade(args))
