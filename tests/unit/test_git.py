"""Tests for the git helpers."""

from __future__ import annotations

import pytest

from agent_backbone.git import git_write_paths, parse_github_remote


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


class TestGitWritePaths:
    def test_directory_symlink_loop_has_no_grants(self, tmp_path):
        loop = tmp_path / "loop"
        loop.symlink_to(loop, target_is_directory=True)
        assert git_write_paths(loop) == ()

    def _worktree(self, tmp_path):
        common = tmp_path / "main" / ".git"
        private = common / "worktrees" / "wt"
        private.mkdir(parents=True)
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {private}\n")
        (private / "gitdir").write_text(str(wt / ".git"))
        (private / "commondir").write_text("../..\n")
        return wt, common, private

    @pytest.mark.parametrize("relative", [False, True])
    def test_validated_worktree_keeps_shared_objects_and_private_index(self, tmp_path, relative):
        wt, common, private = self._worktree(tmp_path)
        if relative:
            (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
        paths = git_write_paths(wt)
        assert str(common / "objects") in paths
        assert str(common / "refs") in paths
        assert str(private / "index.lock") in paths
        assert str(private / "HEAD") in paths
        assert str(common) not in paths and str(private) not in paths
        assert not any("config" in p or "hooks" in p for p in paths)

    def test_plain_checkout_opens_commit_paths_only(self, tmp_path):
        gitdir = tmp_path / ".git"
        gitdir.mkdir()
        paths = git_write_paths(tmp_path)
        assert str(gitdir / "index.lock") in paths
        assert str(gitdir / "AUTO_MERGE.lock") in paths
        assert str(gitdir / "MERGE_RR.lock") in paths
        assert str(gitdir / "objects") in paths
        assert str(gitdir) not in paths
        assert not any("config" in p or "hooks" in p for p in paths)

    @pytest.mark.parametrize("metadata", ["gitdir", "commondir"])
    def test_worktree_without_reciprocal_metadata_is_rejected(self, tmp_path, metadata):
        wt, _, private = self._worktree(tmp_path)
        (private / metadata).write_text("/unrelated\n")
        assert git_write_paths(wt) == ()

    def test_forged_absolute_pointer_is_rejected(self, tmp_path):
        (tmp_path / ".git").write_text("gitdir: /sensitive/worktrees/name\n")
        assert git_write_paths(tmp_path) == ()

    def test_external_git_symlink_is_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".git").symlink_to(outside, target_is_directory=True)
        assert git_write_paths(repo) == ()

    def test_a_granted_path_cannot_alias_git_config(self, tmp_path):
        gitdir = tmp_path / ".git"
        gitdir.mkdir()
        (gitdir / "config").write_text("")
        (gitdir / "index").symlink_to(gitdir / "config")
        paths = git_write_paths(tmp_path)
        assert str(gitdir / "index") not in paths
        assert str(gitdir / "config") not in paths

    @pytest.mark.parametrize("pointer", [None, "not a pointer", "gitdir: /super/.git/modules/sub"])
    def test_non_repositories_and_unexpected_pointers_have_no_grants(self, tmp_path, pointer):
        if pointer is not None:
            (tmp_path / ".git").write_text(pointer)
        assert git_write_paths(tmp_path) == ()
