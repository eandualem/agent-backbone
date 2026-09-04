"""The swarm worktree against a real temporary repository.

A worktree whose directory was deleted by hand stays registered with git
and used to block the swarm name for good; teardown now prunes it and a
new swarm with that name can be created again.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_backbone.services.swarm._worktree import (
    create_worktree,
    is_registered,
    remove_worktree,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture(autouse=True)
def no_real_tmux():
    """Override the suite-wide guard: these tests run real git, never tmux."""
    yield


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    identity = ("-c", "user.email=t@example.com", "-c", "user.name=t")
    _git(repo, *identity, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


async def test_a_deleted_worktree_is_pruned_and_the_name_is_reusable(repo: Path):
    worktree, branch = await create_worktree(repo, "sw")
    assert worktree.is_dir() and branch == "swarm/sw"
    shutil.rmtree(worktree)  # someone deleted the files by hand
    assert await is_registered(repo, worktree)  # git still lists it

    assert await remove_worktree(repo, worktree) is True
    assert not await is_registered(repo, worktree)

    again, _ = await create_worktree(repo, "sw")  # the branch survived; the name is free
    assert again.is_dir()
    assert await remove_worktree(repo, again) is True


async def test_a_missing_and_still_registered_worktree_does_not_block_creation(repo: Path):
    worktree, _ = await create_worktree(repo, "sw")
    shutil.rmtree(worktree)
    # No teardown ran: creation itself must cope with "already registered".
    again, _ = await create_worktree(repo, "sw")
    assert again.is_dir()


async def test_a_git_failure_never_reads_as_unregistered(repo: Path, monkeypatch):
    """`worktree list` failing must not let teardown claim the registration is gone."""
    from agent_backbone.services.swarm import _worktree

    worktree, _ = await create_worktree(repo, "sw")
    shutil.rmtree(worktree)

    async def _broken(directory, *args, **kwargs):
        # `remove` fails (as it does for a corrupted registration) and the
        # listing that would confirm the prune fails too.
        if args[:2] in (("worktree", "list"), ("worktree", "remove")):
            return 128, "", "fatal: git broke"
        return await _worktree_run_git(directory, *args, **kwargs)

    _worktree_run_git = _worktree.run_git
    monkeypatch.setattr(_worktree, "run_git", _broken)
    assert await is_registered(repo, worktree) is None
    assert await remove_worktree(repo, worktree) is False


async def test_a_real_git_error_is_still_reported(repo: Path, tmp_path: Path):
    stranger = tmp_path / "not-a-worktree"
    stranger.mkdir()
    assert await remove_worktree(repo, stranger) is False
