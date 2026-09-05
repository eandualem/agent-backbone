"""Tests for the git helpers."""

from __future__ import annotations

import pytest

from agent_backbone.git import parse_github_remote, worktree_git_dir


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/app", "acme/app"),
        ("https://github.com/acme/app.git", "acme/app"),
        ("https://user@github.com/acme/app.git", "acme/app"),
        ("git@github.com:acme/app.git", "acme/app"),
        ("ssh://git@github.com/acme/app", "acme/app"),
        ("https://github.com/acme/app/", "acme/app"),
        # only github.com itself: lookalike hosts must not become "acme/app"
        ("https://evilgithub.com/acme/app", ""),
        ("https://github.com.evil.example/acme/app", ""),
        ("git@gitlab.com:acme/app.git", ""),
        ("https://example.com/github.com/acme/app", ""),
        ("", ""),
    ],
)
def test_parse_github_remote(url, expected):
    assert parse_github_remote(url) == expected


class TestWorktreeGitDir:
    """A linked worktree's git metadata lives under the main checkout's .git."""

    def test_linked_worktree_points_at_the_main_git_dir(self, tmp_path):
        main_git = tmp_path / "main" / ".git"
        (main_git / "worktrees" / "wt").mkdir(parents=True)
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {main_git / 'worktrees' / 'wt'}\n")
        assert worktree_git_dir(wt) == main_git

    def test_relative_gitdir_is_resolved_against_the_worktree(self, tmp_path):
        main_git = tmp_path / "main" / ".git"
        (main_git / "worktrees" / "wt").mkdir(parents=True)
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
        assert worktree_git_dir(wt) == main_git.resolve()

    def test_plain_checkout_and_non_repo_have_nothing_to_open(self, tmp_path):
        plain = tmp_path / "plain"
        (plain / ".git").mkdir(parents=True)
        assert worktree_git_dir(plain) is None
        assert worktree_git_dir(tmp_path / "nowhere") is None

    def test_unexpected_pointer_is_ignored(self, tmp_path):
        # A submodule's .git file points at <super>/.git/modules/<name>: not a
        # worktree, and not a directory the backbone should open.
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".git").write_text("gitdir: /elsewhere/.git/modules/sub\n")
        assert worktree_git_dir(sub) is None
        (sub / ".git").write_text("not a pointer\n")
        assert worktree_git_dir(sub) is None
