"""Git worktree lifecycle for swarms — one shared worktree + branch per swarm."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SWARM_SUBDIR = ".backbone/swarms"


async def _git(repo_dir: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_dir),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode().strip(), err.decode().strip()


async def is_git_repo(directory: Path) -> bool:
    rc, _, _ = await _git(directory, "rev-parse", "--git-dir")
    return rc == 0


async def _exclude_swarm_dir(repo_dir: Path) -> None:
    """Keep `.backbone/` out of git status without touching tracked files."""
    rc, common, _ = await _git(repo_dir, "rev-parse", "--git-common-dir")
    if rc != 0:
        return
    common_dir = Path(common) if Path(common).is_absolute() else repo_dir / common
    exclude = common_dir / "info" / "exclude"
    try:
        existing = exclude.read_text() if exclude.is_file() else ""
        if ".backbone/" not in existing:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(existing.rstrip("\n") + "\n.backbone/\n")
    except OSError:
        log.warning("Could not add .backbone/ to %s (non-fatal)", exclude)


async def create_worktree(repo_dir: Path, swarm: str) -> tuple[Path, str]:
    """Create the swarm's worktree and branch inside the initiator's repository.

    Returns ``(worktree_path, branch)``. Raises RuntimeError with git's own
    message when the worktree or branch cannot be created.
    """
    branch = f"swarm/{swarm}"
    worktree = repo_dir / SWARM_SUBDIR / swarm
    await _exclude_swarm_dir(repo_dir)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = await _git(repo_dir, "worktree", "add", str(worktree), "-b", branch)
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {err}")
    log.info("Created swarm worktree %s (branch %s)", worktree, branch)
    return worktree, branch


async def remove_worktree(repo_dir: Path, worktree: Path) -> bool:
    """Remove the swarm worktree (the branch is kept — history is never destroyed)."""
    rc, _, err = await _git(repo_dir, "worktree", "remove", "--force", str(worktree))
    if rc != 0:
        log.warning("git worktree remove failed for %s: %s", worktree, err)
        return False
    log.info("Removed swarm worktree %s", worktree)
    return True
