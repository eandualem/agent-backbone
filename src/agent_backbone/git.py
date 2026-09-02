"""Running ``git`` without blocking the event loop."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://(?:[^@/]+@)?github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
"""The remote forms GitHub hands out (https, ssh, scp-style), and only for
``github.com`` itself — ``evilgithub.com/acme/app`` is not ``acme/app``."""


async def run_git(repo_dir: Path | str, *args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """``git -C repo_dir *args`` → ``(returncode, stdout, stderr)``, stripped.

    A missing binary or a timeout reads as a failed command (``rc`` 1).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_dir),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 1, "", str(exc)
    try:
        async with asyncio.timeout(timeout):
            out, err = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "", f"git {' '.join(args)} timed out"
    return proc.returncode or 0, out.decode().strip(), err.decode().strip()


def parse_github_remote(url: str) -> str:
    """``owner/name`` from an https or ssh GitHub remote, else ``""``."""
    match = _GITHUB_REMOTE_RE.match(url.strip())
    return f"{match.group('owner')}/{match.group('repo')}" if match else ""


async def detect_repo(directory: Path) -> str:
    """The GitHub ``owner/name`` of a directory's ``origin`` remote, if any."""
    rc, out, _ = await run_git(directory, "remote", "get-url", "origin", timeout=5)
    return parse_github_remote(out) if rc == 0 else ""
