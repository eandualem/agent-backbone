"""Provision local CLI credentials without starting or interrupting any runtime."""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import stat
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from agent_backbone.config import (
    BackboneConfig,
    agent_api_key,
    agent_credential,
    agent_profile_path,
    read_agent_profile,
)
from agent_backbone.services.agents._file_reader import read_launch_marker


def _write_profile(path, content: str) -> None:
    existing = None
    try:
        existing = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(existing)
        encoded = content.encode()
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size == len(encoded)
            and os.read(existing, len(encoded) + 1) == encoded
        ):
            return
    except OSError:
        pass
    finally:
        if existing is not None:
            os.close(existing)
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _profile_root(data_dir: Path) -> Path:
    root = data_dir / "agent-credentials"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Agent credential profile directory must be private")
    return root


def _open_profile_lock(data_dir: Path) -> int:
    root = _profile_root(data_dir)
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("Agent credential profile lock must be a private regular file")
    return fd


@asynccontextmanager
async def profile_lock(data_dir: Path):
    """Serialize authoritative DB reads and profile writes across processes.

    Poll without blocking the event loop; cancellation closes the descriptor.
    Keep the lock inode stable so every writer coordinates on the same file.
    """
    fd = _open_profile_lock(data_dir)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _profile_values(config: BackboneConfig, name: str, identity: str, launch: str) -> dict:
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(config.backbone.host, config.backbone.host)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host):
        raise ValueError("Agent credential profiles require a valid API hostname")
    return {
        "agent": name,
        "registration": identity,
        "launch": launch,
        "host": host,
        "port": str(config.backbone.port),
        "credential": agent_api_key(config.api_key),
    }


def _provision_agent_profiles(config: BackboneConfig, registrations: list[dict]) -> None:
    """Caller holds profile_lock from before reading the authoritative snapshot."""
    if agent_credential(config.api_key) or (
        not config.api_key and not config.security.allow_unauthenticated
    ):
        return
    root = config.data_dir / "agent-credentials"
    keep = set()
    for row in registrations:
        name, identity = row["name"], row.get("report_identity")
        if not identity:
            continue
        path = agent_profile_path(config.data_dir, name)
        keep.add(path)
        launch = read_launch_marker(config.state_dir, name)
        try:
            created = datetime.fromisoformat(row["created_at"]).timestamp()
            launch_id = launch[1] if launch and launch[0] >= created else ""
        except (KeyError, TypeError, ValueError):
            launch_id = ""
        values = _profile_values(config, name, identity, launch_id)
        try:
            current = read_agent_profile(config.data_dir, name)
        except ValueError:
            current = None
        if current != values:
            # Never inherit a permissive mode from an older or malformed file.
            _write_profile(path, "".join(f"{key}={value}\n" for key, value in values.items()))
    for path in root.glob("*.env"):
        if path not in keep:
            # Retain a refusal for old processes, even if their environment still
            # contains a formerly valid shared collaboration credential.
            _write_profile(path, "retired=true\n")


def _bind_agent_profile(config: BackboneConfig, registration: dict, launch_id: str) -> None:
    """Caller holds profile_lock from before reading the registration."""
    if agent_credential(config.api_key) or (
        not config.api_key and not config.security.allow_unauthenticated
    ):
        return
    name, identity = registration["name"], registration["report_identity"]
    try:
        profile = read_agent_profile(config.data_dir, name)
    except ValueError:
        profile = None
    if profile is None:
        profile = _profile_values(config, name, identity, launch_id)
    else:
        profile.update(registration=identity, launch=launch_id)
        if not config.api_key or not profile["credential"]:
            profile["credential"] = agent_api_key(config.api_key)
    _write_profile(
        agent_profile_path(config.data_dir, name),
        "".join(f"{key}={value}\n" for key, value in profile.items()),
    )


def agent_profile_readiness(config: BackboneConfig, row: dict) -> str:
    """Nonsecret evidence for current uptake and the next ordinary launch."""
    try:
        profile = read_agent_profile(config.data_dir, row["name"])
        if not profile or profile["registration"] != row.get("report_identity"):
            return "unavailable"
        if profile["credential"] != agent_api_key(config.api_key):
            return "unavailable"
        launch = read_launch_marker(config.state_dir, row["name"])
        if not launch:
            return "future-launch-only"
        if not profile["launch"] or profile["launch"] != launch[1]:
            return "uncertain-legacy"
        return "ready-existing-launch"
    except (OSError, ValueError):
        return "unavailable"
