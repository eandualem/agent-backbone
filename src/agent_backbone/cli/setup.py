"""``backbone init``, ``secrets`` and ``doctor`` — getting a machine ready."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

from agent_backbone.cli import _common
from agent_backbone.config import (
    SECRET_ENV_KEYS,
    bootstrap_config,
    env_file_keys,
)

log = logging.getLogger(__name__)

_EXAMPLE_ENV = """\
# Secrets for agent-backbone (never commit this file)
BACKBONE_API_KEY={api_key}

# GitHub — a token (PAT or `gh auth token`) is the simplest option
# GITHUB_TOKEN=ghp_...
# GITHUB_WEBHOOK_SECRET=...           # set this and the backbone switches to webhook intake

# GitHub App (alternative to GITHUB_TOKEN)
# GITHUB_APP_ID=
# GITHUB_APP_PRIVATE_KEY_PATH=

# Telegram bot token from @BotFather
# TELEGRAM_TOKEN=
"""


def cmd_init(args: argparse.Namespace) -> int:
    config = bootstrap_config(args.data_dir)
    data_dir = config.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "state").mkdir(exist_ok=True)

    env_path = config.env_path
    if not env_path.exists() or args.force:
        # Created (or truncated) with mode 0600 before the key is written, so
        # no umask and no earlier permissive mode ever exposes it.
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fd, 0o600)
            fh.write(_EXAMPLE_ENV.format(api_key=secrets.token_urlsafe(32)))
        print(f"wrote {env_path} (contains a generated BACKBONE_API_KEY)")
    else:
        print(f"{env_path} exists (kept; use --force to regenerate)")

    async def _migrate() -> None:
        async with _common.Direct(config):
            pass

    asyncio.run(_migrate())
    print(f"database ready: {config.database_url}")

    print("\nNext steps:")
    print("  1. backbone doctor")
    print("  2. backbone up --detach")
    print("  3. cd ~/code/my-app && backbone agent start")
    print(f"\nTokens (GitHub, Telegram) go in {env_path} — `backbone secrets set TELEGRAM_TOKEN`.")
    return 0


_SECRET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _env_keys(env_path: Path) -> set[str]:
    """Keys with a live (uncommented) assignment in ``.env``."""
    return set(env_file_keys(env_path))


def _write_env_value(env_path: Path, key: str, value: str | None) -> str:
    """Set (or, with ``value=None``, remove) ``KEY`` in ``.env`` atomically, mode 0600.

    A live ``KEY=…`` line is replaced in place; a commented ``# KEY=`` placeholder
    (``backbone init`` writes those) is turned into the assignment; otherwise the
    line is appended. Returns ``replaced`` / ``added`` / ``removed`` / ``absent``.
    """
    import fcntl

    env_path.parent.mkdir(parents=True, exist_ok=True)
    # One writer at a time for the whole read-modify-write: two concurrent
    # `secrets set` calls must not lose each other's key.
    with open(env_path.with_name(".env.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _write_env_value_locked(env_path, key, value)


def _write_env_value_locked(env_path: Path, key: str, value: str | None) -> str:
    lines = env_path.read_text().splitlines() if env_path.is_file() else []
    out: list[str] = []
    action = "absent" if value is None else "added"
    for line in lines:
        stripped = line.strip()
        bare = stripped.lstrip("#").strip()
        if bare.startswith(f"{key}=") and action in ("added", "absent"):
            if value is None:
                if not stripped.startswith("#"):
                    action = "removed"
                    continue
            else:
                action = "replaced" if not stripped.startswith("#") else "added"
                out.append(f"{key}={value}")
                continue
        out.append(line)
    if value is not None and action == "added" and not any(ln.startswith(f"{key}=") for ln in out):
        out.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_path.with_name(f".env.{os.getpid()}.tmp")
    tmp.write_text("\n".join(out) + ("\n" if out else ""))
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
    os.chmod(env_path, 0o600)
    return action


def cmd_secrets(args: argparse.Namespace) -> int:
    """Manage ``<data_dir>/.env`` — the only secrets file the backbone reads.

    Tokens never live in a project directory: the backbone runs `agent start`
    *inside* your repositories, which have their own `.env` files, so it
    reads exactly one file of its own. This command makes that file easy to
    find and safe to edit (values prompted, not typed into shell history).
    """
    env_path = bootstrap_config().env_path
    sub = args.secrets_command
    if sub == "path":
        print(env_path)
        return 0
    if sub == "list":
        present = _env_keys(env_path)
        for key in SECRET_ENV_KEYS:
            print(f"  {'✓' if key in present else '-'} {key}")
        for key in sorted(present - set(SECRET_ENV_KEYS)):
            print(f"  ✓ {key}")
        print(f"\n{env_path}" if env_path.is_file() else f"\n{env_path} (not created yet)")
        return 0

    key = args.key.strip().upper()
    if not _SECRET_KEY_RE.match(key):
        print(f"invalid key {args.key!r} (use UPPER_SNAKE_CASE, e.g. TELEGRAM_TOKEN)")
        return 1
    if sub == "unset":
        action = _write_env_value(env_path, key, None)
        print(f"{action} {key} in {env_path}" if action == "removed" else f"{key} was not set")
        return 0

    value = args.value
    if value is None:
        if sys.stdin.isatty():
            value = getpass.getpass(f"{key}: ")
        else:
            value = sys.stdin.readline().rstrip("\n")
    value = value.strip()
    if not value:
        print("empty value — nothing written")
        return 1
    action = _write_env_value(env_path, key, value)
    print(f"{action} {key} in {env_path} (mode 0600)")
    print("The running backbone reads .env at startup: `backbone down && backbone up --detach`.")
    return 0


def cmd_runtimes(args: argparse.Namespace) -> int:
    """Every runtime, whether its binary is installed, and example model ids."""
    from agent_backbone.services.runtimes import RUNTIMES as REGISTRY

    for rt in REGISTRY.values():
        installed = "installed" if rt.available() else "not found"
        models = ", ".join(rt.models) if rt.models else "use the CLI's own model picker"
        print(
            f"  {rt.id:<10s} {rt.display_name:<12s} {installed:<10s} "
            f"state: {rt.reports_state:<17s} models: {models}"
        )
    print("\n`--model` is passed to the CLI verbatim; these are examples, not a complete list.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from agent_backbone.services.runtimes import RUNTIMES as REGISTRY

    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        mark = "✓" if passed else "✗"
        print(f"  {mark} {label}" + (f"  — {hint}" if (hint and not passed) else ""))
        ok = ok and passed

    async def run() -> int:
        nonlocal ok
        boot = bootstrap_config()
        print("Storage")
        check(f"data dir exists: {boot.data_dir}", boot.data_dir.is_dir(), "run `backbone init`")
        check(f".env present: {boot.env_path}", boot.env_path.is_file(), "run `backbone init`")
        try:
            async with _common.Direct(boot) as direct:
                config = direct.config
                check(f"database reachable: {config.database_url}", True)
        except Exception as exc:
            check(f"database reachable: {boot.database_url}", False, f"{exc}; run `backbone init`")
            return 1

        print("Agents")
        if not config.agents:
            print("  - none yet (run `backbone agent start` from a project directory)")
        for spec in config.agents:
            check(f"'{spec.name}' dir exists: {spec.path}", spec.path.is_dir())
            installed = spec.runtime in REGISTRY and REGISTRY[spec.runtime].available()
            check(f"'{spec.name}' runtime '{spec.runtime}' installed", installed)
            if not spec.repo:
                print(f"  ! '{spec.name}' has no GitHub remote — issue routing is off for it")

        print("Tools")
        check("tmux on PATH", shutil.which("tmux") is not None, "install tmux")
        found = [rt.id for rt in REGISTRY.values() if rt.binary and rt.available()]
        print(f"  - runtimes installed: {', '.join(found) or 'none'}")

        print("Security")
        check(
            "API key configured",
            bool(config.api_key) or config.security.allow_unauthenticated,
            "set BACKBONE_API_KEY in .env",
        )
        if config.security.allow_unauthenticated:
            print("  ! API authentication is disabled (security.allow_unauthenticated)")

        print("Integrations")
        if config.github_app_ready and not config.github_token:
            try:
                import cryptography  # noqa: F401
            except ModuleNotFoundError:
                check(
                    "GitHub App auth dependencies installed",
                    False,
                    "install the extra: uv tool install 'agent-backbone[github-app]'",
                )
            key_ok = Path(config.github_app_private_key_path).expanduser().is_file()
            check(f"GitHub App private key: {config.github_app_private_key_path}", key_ok)
        if config.github_ready:
            print(f"  ✓ GitHub credentials found — intake: {config.github_intake}")
            if config.github_intake == "poll":
                print(
                    "    (set GITHUB_WEBHOOK_SECRET + expose /webhooks/github for instant delivery)"
                )
        else:
            print(
                "  - GitHub not configured (optional): `backbone secrets set GITHUB_TOKEN` "
                f"(writes {config.env_path})"
            )
        if config.telegram_ready:
            check(
                "Telegram allowed_chat_ids set",
                bool(config.telegram.allowed_chat_ids),
                "backbone config set telegram.allowed_chat_ids '[<chat id>]'",
            )
        else:
            print(
                "  - Telegram not configured (optional): `backbone secrets set TELEGRAM_TOKEN` "
                f"(writes {config.env_path})"
            )

        print("Backbone")
        api_state = "up" if await _common.api_up(config) else "down"
        print(f"  - API: {api_state} ({_common.api_url(config, '')})")
        print("\nAll good." if ok else "\nSome checks failed.")
        return 0 if ok else 1

    return asyncio.run(run())
