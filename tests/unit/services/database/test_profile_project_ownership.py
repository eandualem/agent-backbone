"""Internal project claims reject cross-profile effects before materialization."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from agent_backbone.services.database import ProfileIdentity, ProfileRegistry, ProfileReservation
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.skills import Skill, manifest_path, materialize


@pytest.fixture
def profiles(tmp_path):
    registry = ProfileRegistry.create(tmp_path / "control" / "registry.db")
    owners = []
    for name in ("a", "b"):
        owner = ProfileReservation(
            ProfileIdentity(f"profile-{name}", f"installation-{name}", f"lifecycle-{name}"),
            (tmp_path / name).resolve(),
            name,
            "test-v1",
        )
        registry.reserve(owner)
        owners.append(owner)
    return registry, owners


def make_repo(path):
    (path / ".git" / "info").mkdir(parents=True)
    (path / ".git" / "info" / "exclude").write_text("# original\n")
    return path


def apply(registry, owner, repo, runtime, *, release=False):
    store = owner.data_dir / "skills" / "store"
    skill = store / "example"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"content from {owner.display_name}")
    return materialize(
        store,
        repo,
        () if release else runtime.skill_dirs,
        [] if release else [Skill("example", skill)],
        manifest_path(owner.data_dir, "worker"),
        read=runtime.skill_read_dirs,
        claim_projects=partial(registry.claim_projects, owner.identity, owner.data_dir),
    )


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_other_profile_cannot_reuse_or_release_managed_links(profiles, tmp_path, runtime):
    registry, (a, b) = profiles
    repo = make_repo(tmp_path / "project")
    rt = RUNTIMES[runtime]
    assert apply(registry, a, repo, rt).ok
    link = repo / rt.skill_dirs[0] / "example"
    before = link.readlink(), (repo / ".git/info/exclude").read_bytes()
    rejected = apply(registry, b, repo, rt)
    assert not rejected.ok and "separate checkout" in rejected.broken[0]
    assert (link.readlink(), (repo / ".git/info/exclude").read_bytes()) == before
    assert not manifest_path(b.data_dir, "worker").exists()
    # Same-profile agents can still share, and normal release retains ownership.
    assert apply(registry, a, repo, rt).ok
    assert apply(registry, a, repo, rt, release=True).ok
    assert not link.exists()
    assert not apply(registry, b, repo, rt).ok


def test_linked_worktrees_conflict_on_common_git_directory(profiles, tmp_path):
    registry, (a, b) = profiles
    main = make_repo(tmp_path / "main")
    worktree = tmp_path / "linked"
    worktree.mkdir()
    private = main / ".git/worktrees/linked"
    private.mkdir(parents=True)
    (private / "commondir").write_text("../..\n")
    (worktree / ".git").write_text(f"gitdir: {private}\n")
    rt = RUNTIMES["codex"]
    assert apply(registry, a, main, rt).ok
    before = (main / ".git/info/exclude").read_bytes()
    rejected = apply(registry, b, worktree, rt)
    assert not rejected.ok and "separate checkout" in rejected.broken[0]
    assert (main / ".git/info/exclude").read_bytes() == before
    assert not (worktree / ".agents").exists()


def test_conflict_precedes_old_checkout_cleanup(profiles, tmp_path):
    registry, (a, b) = profiles
    old, new = (make_repo(tmp_path / name) for name in ("old", "new"))
    rt = RUNTIMES["claude"]
    assert apply(registry, a, old, rt).ok
    assert apply(registry, b, new, rt).ok
    manifest = manifest_path(a.data_dir, "worker")
    before = manifest.read_bytes(), (old / ".git/info/exclude").read_bytes()
    assert not apply(registry, a, new, rt).ok
    assert (old / rt.skill_dirs[0] / "example/SKILL.md").read_text() == "content from a"
    assert (manifest.read_bytes(), (old / ".git/info/exclude").read_bytes()) == before
    # A conflicting old manifest must also be checked when the new path is free.
    body = json.loads(manifest.read_text())
    body["repo"] = str(new)
    manifest.write_text(json.dumps(body))
    free = make_repo(tmp_path / "free")
    assert not apply(registry, a, free, rt).ok
    assert not (free / rt.skill_dirs[0]).exists()
    registry.claim_projects(b.identity, b.data_dir, (free,))  # no partial claim


def test_concurrent_overlapping_claims_have_one_winner_and_no_partial_write(profiles, tmp_path):
    registry, (a, b) = profiles
    shared = make_repo(tmp_path / "shared")
    distinct = [make_repo(tmp_path / name) for name in ("one", "two")]

    def claim(pair):
        owner, extra = pair
        try:
            registry.claim_projects(owner.identity, owner.data_dir, (extra, shared))
            return owner
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, zip((a, b), distinct, strict=True)))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    loser_index = results.index(None)
    registry.claim_projects(winner.identity, winner.data_dir, (distinct[loser_index],))


def test_aliases_nested_paths_and_failed_lifecycles_keep_claims(profiles, tmp_path):
    registry, (a, b) = profiles
    repo = make_repo(tmp_path / "project")
    registry.claim_projects(a.identity, a.data_dir, (repo,))
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    nested = repo / "nested"
    nested.mkdir()
    for path in (alias, nested, tmp_path):
        with pytest.raises(ValueError, match="conflict|another profile"):
            registry.claim_projects(b.identity, b.data_dir, (path,))
    registry.invalidate(a.identity, a.data_dir)
    with pytest.raises(ValueError, match="invalidated"):
        registry.claim_projects(a.identity, a.data_dir, (repo,))
    with pytest.raises(ValueError, match="another profile"):
        ProfileRegistry(registry.path).claim_projects(b.identity, b.data_dir, (repo,))


def test_old_registry_needs_explicit_initialization_not_discovery(profiles, tmp_path):
    registry, (a, _) = profiles
    repo = make_repo(tmp_path / "project")
    with sqlite3.connect(registry.path) as conn:
        conn.execute("DROP TABLE project_claims")
        conn.execute("PRAGMA user_version = 1")
    before = registry.path.read_bytes()
    assert registry.show(a.identity.profile_id)["binding"] is None
    assert registry.path.read_bytes() == before
    with pytest.raises(ValueError, match="initialization"):
        registry.claim_projects(a.identity, a.data_dir, (repo,))
    ProfileRegistry.create(registry.path).claim_projects(a.identity, a.data_dir, (repo,))
    assert registry.check_reservation(a.identity, a.data_dir) == a


def test_external_git_exclude_fails_before_cleanup(profiles, tmp_path):
    registry, (a, _) = profiles
    repo = make_repo(tmp_path / "project")
    rt = RUNTIMES["codex"]
    assert apply(registry, a, repo, rt).ok
    exclude = repo / ".git/info/exclude"
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    exclude.unlink()
    exclude.symlink_to(outside)
    assert not apply(registry, a, repo, rt, release=True).ok
    assert outside.read_text() == "preserve"
    assert (repo / rt.skill_dirs[0] / "example").is_symlink()
