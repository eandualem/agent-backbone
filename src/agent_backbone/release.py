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
    """What code a fresh process would run: the checkout's commit for an
    editable install, the installed version otherwise. When this differs
    from what the running backbone started with, a restart runs new code."""
    install = install or installation()
    if install.kind == "editable" and install.path:
        try:
            result = subprocess.run(
                ["git", "-C", install.path, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return f"git:{result.stdout.strip()}"
    return f"version:{installed_version()}"


def latest_published(timeout: float = 5.0) -> str | None:
    """The newest version on PyPI, or None when it cannot be reached."""
    import httpx

    try:
        resp = httpx.get(f"https://pypi.org/pypi/{PACKAGE}/json", timeout=timeout)
        resp.raise_for_status()
        return str(resp.json()["info"]["version"])
    except Exception:
        return None
