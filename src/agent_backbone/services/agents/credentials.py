"""Provision local CLI credentials without starting or interrupting any runtime."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from datetime import datetime

from agent_backbone.config import (
    BackboneConfig,
    agent_api_key,
    agent_credential,
    agent_profile_path,
    read_agent_profile,
)
from agent_backbone.services.agents._file_reader import read_launch_marker


def _write_profile(path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provision_agent_profiles(config: BackboneConfig, registrations: list[dict]) -> None:
    """Refresh all registered agents, including offline ones, from operator configuration.

    Old launches are adopted only when their managed marker postdates the current
    registration. Forget/recreate therefore cannot assign a surviving old marker
    to the replacement. New launches write their marker before provisioning.
    """
    if not config.api_key or agent_credential(config.api_key):
        return
    root = config.data_dir / "agent-credentials"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Agent credential profile directory must be private")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(config.backbone.host, config.backbone.host)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host):
        raise ValueError("Agent credential profiles require a valid API hostname")
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
        values = {
            "agent": name,
            "registration": identity,
            "launch": launch_id,
            "host": host,
            "port": str(config.backbone.port),
            "credential": agent_api_key(config.api_key),
        }
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
