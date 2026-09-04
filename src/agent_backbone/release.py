"""Which installation this is, what code it runs, and how to upgrade it.

A leaf: standard library only (PyPI is consulted lazily through httpx).
``backbone upgrade`` and the running backbone's restart-on-upgrade watch
both read from here, so they agree on what "the code changed" means.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

PACKAGE = "agent-backbone"


@dataclass(frozen=True)
class Installation:
    """How this package got onto the machine."""

    kind: str
    """``editable`` (a development checkout), ``uv`` (``uv tool``), ``pipx`` or ``other``."""
    path: str | None = None
    """The checkout an editable install runs from."""

    @property
    def upgrade_command(self) -> list[str] | None:
        """The installer's own upgrade command, or None when there is none to run."""
        if self.kind == "uv":
            return ["uv", "tool", "upgrade", PACKAGE]
        if self.kind == "pipx":
            return ["pipx", "upgrade", PACKAGE]
        return None

    def describe(self) -> str:
        if self.kind == "editable":
            return f"development checkout at {self.path or '?'}"
        if self.kind == "uv":
            return "uv tool"
        if self.kind == "pipx":
            return "pipx"
        return f"unknown installer ({sys.executable})"


def _direct_url() -> dict | None:
    try:
        text = metadata.distribution(PACKAGE).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def installation(executable: str | None = None) -> Installation:
    """Detect the installer from the distribution's ``direct_url.json`` and the interpreter path."""
    direct = _direct_url()
    if direct and (direct.get("dir_info") or {}).get("editable"):
        url = str(direct.get("url", ""))
        path = unquote(urlparse(url).path) if url.startswith("file:") else None
        return Installation("editable", path)
    parts = Path(executable or sys.executable).parts
    if "uv" in parts and "tools" in parts:
        return Installation("uv")
    if "pipx" in parts:
        return Installation("pipx")
    return Installation("other")


def installed_version() -> str:
    """The version on disk right now (``unknown`` when the distribution is gone)."""
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return "unknown"


def code_identity(install: Installation | None = None) -> str:
    """What code a fresh process would run: ``git:<branch>@<commit>`` for an
    editable checkout, ``version:<installed>`` otherwise. When this differs
    from what the running backbone started with, a restart runs new code."""
    install = install or installation()
    if install.kind == "editable" and install.path:
        checkout = _checkout(install.path)
        if checkout:
            branch, commit = checkout
            return f"git:{branch}@{commit}"
    return f"version:{installed_version()}"


def _checkout(path: str) -> tuple[str, str] | None:
    """``(branch, commit)`` read from one ``git status`` so they belong to the
    same checkout state (two reads could straddle a branch switch)."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "status", "--porcelain=v2", "--branch"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("# branch."):
            key, _, value = line[2:].partition(" ")
            fields[key] = value.strip()
    branch, commit = fields.get("branch.head"), fields.get("branch.oid")
    if not branch or not commit or commit == "(initial)":
        return None
    return branch, commit


def same_line(started: str, current: str) -> bool:
    """Whether ``current`` is a newer state of the *same* thing as ``started``.

    A checkout on another branch is development, not an upgrade: the
    backbone keeps running until the branch it started on moves (a pull
    into it, or a switch back to it with new commits).
    """
    if started.startswith("git:") and current.startswith("git:"):
        # The commit is last and never contains "@"; a branch name may.
        return started[4:].rsplit("@", 1)[0] == current[4:].rsplit("@", 1)[0]
    return True


def latest_published(timeout: float = 5.0) -> str | None:
    """The newest version on PyPI, or None when it cannot be reached."""
    import httpx

    try:
        resp = httpx.get(f"https://pypi.org/pypi/{PACKAGE}/json", timeout=timeout)
        resp.raise_for_status()
        return str(resp.json()["info"]["version"])
    except Exception:
        return None
