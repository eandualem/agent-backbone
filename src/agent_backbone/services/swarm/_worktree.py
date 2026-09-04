"""Git worktree lifecycle for swarms — one shared worktree + branch per swarm."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_backbone.git import run_git

log = logging.getLogger(__name__)

SWARM_SUBDIR = ".backbone/swarms"


async def is_git_repo(directory: Path) -> bool:
    rc, _, _ = await run_git(directory, "rev-parse", "--git-dir")
    return rc == 0


async def current_branch(directory: Path) -> str:
    """The checkout's current branch — the base a swarm's PR must target.

    Raises RuntimeError for a detached HEAD or an unreadable checkout: a
    silently guessed base would make the coordinator target the wrong branch.
    """
    rc, out, _ = await run_git(directory, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or not out:
        raise RuntimeError(f"could not determine the current branch of {directory}")
    if out == "HEAD":
        raise RuntimeError(
            f"{directory} is on a detached HEAD — check out the branch the swarm's "
            "PR should target, then create the swarm again"
        )
    return out


async def _exclude_swarm_dir(repo_dir: Path) -> None:
    """Keep `.backbone/` out of git status without touching tracked files."""
    rc, common, _ = await run_git(repo_dir, "rev-parse", "--git-common-dir")
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
    rc, _, err = await run_git(repo_dir, "worktree", "add", str(worktree), "-b", branch)
    if rc != 0 and "already exists" in err:
        # A previous swarm with this name left its branch behind (branches
        # survive teardown by design) — continue on the existing branch.
        rc, _, err = await run_git(repo_dir, "worktree", "add", str(worktree), branch)
    if rc != 0 and "already registered" in err:
        # The directory is gone but git still lists it: a swarm whose files
        # were deleted by hand. Prune the stale registration and try again.
        await run_git(repo_dir, "worktree", "prune")
        rc, _, err = await run_git(repo_dir, "worktree", "add", str(worktree), branch)
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {err}")
    log.info("Created swarm worktree %s (branch %s)", worktree, branch)
    return worktree, branch


async def is_registered(repo_dir: Path, worktree: Path) -> bool:
    """Whether git still lists ``worktree`` (files present or not)."""
    rc, out, _ = await run_git(repo_dir, "worktree", "list", "--porcelain")
    if rc != 0:
        return False
    target = str(worktree.resolve())
    return any(
        line.startswith("worktree ") and line[9:].strip() == target for line in out.splitlines()
    )


async def remove_worktree(repo_dir: Path, worktree: Path) -> bool:
    """Remove the swarm worktree (the branch is kept — history is never destroyed).

    A worktree whose directory is already gone is still registered with git
    and blocks the name; ``worktree prune`` clears that registration, so the
    result is the same: git no longer lists it.
    """
    rc, _, err = await run_git(repo_dir, "worktree", "remove", "--force", str(worktree))
    if rc == 0:
        log.info("Removed swarm worktree %s", worktree)
        return True
    if not worktree.exists():
        await run_git(repo_dir, "worktree", "prune")
        if not await is_registered(repo_dir, worktree):
            log.info("Pruned the registration of the missing swarm worktree %s", worktree)
            return True
    log.warning("git worktree remove failed for %s: %s", worktree, err)
    return False
