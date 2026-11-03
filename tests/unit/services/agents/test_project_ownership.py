"""Captured ownership gates project effects across the agent and swarm paths."""

import asyncio
import json
import shutil
from contextlib import AsyncExitStack
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec, agents_from_rows, bootstrap_config, sqlite_url
from agent_backbone.services.agents import (
    AgentStore,
    _project_ownership,
    claim_project_directories,
    install_project_ownership,
    project_claims,
    start_agent,
)
from agent_backbone.services.agents.launch import _materialize_skills
from agent_backbone.services.database import (
    BackboneDB,
    ProfileIdentity,
    ProfileRegistry,
    ProfileReservation,
    reconcile_profile_provider,
)
from agent_backbone.services.runtimes import RUNTIMES, _environment
from agent_backbone.services.swarm.interface import create_swarm, teardown_swarm
from agent_backbone.skills import manifest_path


@pytest.fixture
async def owners(tmp_path, monkeypatch):
    monkeypatch.setattr(_project_ownership, "_PROVIDER", None)
    monkeypatch.setattr(_environment, "_CONTEXT", None)
    registry = ProfileRegistry.create(tmp_path / "control/registry.db")
    providers = []
    async with AsyncExitStack() as stack:
        for name in ("a", "b"):
            root = tmp_path / name
            root.mkdir()
            db = await stack.enter_async_context(BackboneDB.connect(sqlite_url(root)))
            identity = ProfileIdentity(f"profile-{name}", await db.signing.audience(), "generation")
            await db.profile_identity.reserve(identity)
            reservation = registry.reserve(ProfileReservation(identity, root, name, "v1"))
            providers.append(
                await reconcile_profile_provider(
                    registry, reservation, root / "backbone.db", root / "tmux/server.sock"
                )
            )
        yield registry, providers


def repo_at(path):
    (path / ".git/info").mkdir(parents=True)
    (path / ".git/info/exclude").write_text("# original\n")
    return path


async def test_identity_is_pinned_and_retained_callback_refuses_invalidation(owners, tmp_path):
    registry, (a, b) = owners
    await install_project_ownership(a)
    await install_project_ownership(a)
    with pytest.raises(ValueError, match="already pinned"):
        await install_project_ownership(b)
    with pytest.raises(ValueError, match="provider directory"):
        project_claims(b.reservation.data_dir)
    claim = project_claims(a.reservation.data_dir)
    project = repo_at(tmp_path / "project")
    registry.invalidate(a.reservation.identity, a.reservation.data_dir)
    with pytest.raises(ValueError, match="invalidated"):
        claim((project,))
    assert (project / ".git/info/exclude").read_text() == "# original\n"


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_conflict_precedes_trust_and_launch_even_without_skills(owners, tmp_path, runtime):
    registry, (a, b) = owners
    project = repo_at(tmp_path / "project")
    registry.claim_projects(b.reservation.identity, b.reservation.data_dir, (project,))
    await install_project_ownership(a)
    config = bootstrap_config(a.reservation.data_dir)
    spec = AgentSpec(name="worker", dir=str(project), runtime=runtime)
    rt = RUNTIMES[runtime]
    with (
        patch("agent_backbone.services.agents.launch.session_exists", return_value=False),
        patch("agent_backbone.services.agents.launch.start_session") as launch,
        patch.object(rt, "pre_trust") as trust,
    ):
        with pytest.raises(ValueError, match="separate checkout"):
            await start_agent(spec, config, wait=False)
        trust.assert_not_called()
        launch.assert_not_called()
    assert not (a.reservation.data_dir / "briefs").exists()


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_rematerialization_and_forget_preserve_other_profiles_links(
    owners, tmp_path, monkeypatch, db, runtime
):
    _, (a, b) = owners
    project = repo_at(tmp_path / "project")
    spec = AgentSpec(name="worker", dir=str(project), runtime=runtime)
    configs = []
    for provider in (a, b):
        config = bootstrap_config(provider.reservation.data_dir)
        store = provider.reservation.data_dir / "skills/store"
        skill = store / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: example\ndescription: Example\n"
            "metadata:\n  backbone-tags: all\n---\nExample"
        )
        configs.append(replace(config, skills=replace(config.skills, store=str(store))))
    rt = RUNTIMES[runtime]
    await install_project_ownership(b)
    assert _materialize_skills(configs[1], spec, rt)[1] is None
    link = project / rt.skill_dirs[0] / "example"
    before = link.readlink(), (project / ".git/info/exclude").read_bytes()
    # A different provider process may have an old record of the same checkout.
    monkeypatch.setattr(_project_ownership, "_PROVIDER", None)
    await install_project_ownership(a)
    assert "separate checkout" in _materialize_skills(configs[0], spec, rt)[1]
    agent_store = AgentStore(db, a.reservation.data_dir)
    await agent_store.start()
    await agent_store.register(spec)
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        with pytest.raises(ValueError, match="separate checkout"):
            await agent_store.forget("worker")
    assert agent_store.agents.get("worker") is not None
    assert (link.readlink(), (project / ".git/info/exclude").read_bytes()) == before
    assert not manifest_path(a.reservation.data_dir, "worker").exists()


async def test_subdirectories_and_linked_worktrees_claim_the_canonical_main(owners, tmp_path):
    registry, (a, b) = owners
    main = repo_at(tmp_path / "main")
    worktree = tmp_path / "linked"
    worktree.mkdir()
    private = main / ".git/worktrees/linked"
    private.mkdir(parents=True)
    (private / "commondir").write_text("../..\n")
    (worktree / ".git").write_text(f"gitdir: {private}\n")
    child = worktree / "subdir"
    child.mkdir()
    await install_project_ownership(a)
    claim_project_directories(a.reservation.data_dir, (child,))
    # Main-checkout settings must not be writable by a competing profile.
    for directory in (main, worktree, child, main / ".git"):
        with pytest.raises(ValueError, match="separate checkout"):
            registry.claim_projects(b.reservation.identity, b.reservation.data_dir, (directory,))


@pytest.mark.parametrize("path_state", ["directory", "missing", "file"])
async def test_forget_uses_current_registration_without_nesting_profile_lock(
    owners, tmp_path, db, path_state
):
    registry, (a, b) = owners
    project = repo_at(tmp_path / "project")
    store = AgentStore(db, a.reservation.data_dir)
    await store.start()
    await store.register(AgentSpec(name="worker", dir=str(project)))
    if path_state != "directory":
        shutil.rmtree(project)
        if path_state == "file":
            project.write_text("replacement file")
    await install_project_ownership(a)
    store._agents = agents_from_rows([])
    store._credential_endpoint = ("127.0.0.1", 9000)
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        assert await asyncio.wait_for(store.forget("worker"), timeout=2)
    assert store.agents.get("worker") is None
    if path_state == "directory":
        with pytest.raises(ValueError, match="separate checkout"):
            registry.claim_projects(b.reservation.identity, b.reservation.data_dir, (project,))


@pytest.mark.parametrize("first_git_dir", ["alpha.git", ".git"])
async def test_separate_git_directories_do_not_claim_the_metadata_parent(
    owners, tmp_path, monkeypatch, first_git_dir
):
    _, providers = owners
    for name, provider in zip(("alpha", "beta"), providers, strict=True):
        repo = tmp_path / name
        repo.mkdir()
        metadata = tmp_path / "metadata" / (first_git_dir if name == "alpha" else "beta.git")
        metadata.mkdir(parents=True)
        (repo / ".git").write_text(f"gitdir: {metadata}\n")
        monkeypatch.setattr(_project_ownership, "_PROVIDER", None)
        await install_project_ownership(provider)
        claim_project_directories(provider.reservation.data_dir, (repo,))


async def test_swarm_conflict_precedes_worktree_creation_and_teardown(owners, tmp_path, db):
    registry, (a, b) = owners
    project = repo_at(tmp_path / "project")
    registry.claim_projects(b.reservation.identity, b.reservation.data_dir, (project,))
    await install_project_ownership(a)
    config = bootstrap_config(a.reservation.data_dir)
    config = replace(
        config,
        agents=agents_from_rows([{"name": "owner", "dir": str(project), "repo": "acme/app"}]),
    )
    store = AgentStore(db, a.reservation.data_dir)
    await store.start()
    gh = AsyncMock()
    gh.get_issue.return_value = AsyncMock(state="open", title="Example")
    with (
        patch("agent_backbone.services.swarm.interface.session_exists", return_value=False),
        patch("agent_backbone.services.swarm.interface.is_git_repo", return_value=True),
        patch("agent_backbone.services.swarm.interface.current_branch", return_value="develop"),
        patch("agent_backbone.services.swarm.interface.create_worktree") as create,
        patch("agent_backbone.services.swarm.interface.remove_worktree") as remove,
        patch("agent_backbone.services.swarm.interface.stop_session") as stop,
    ):
        with pytest.raises(ValueError, match="separate checkout"):
            await create_swarm(
                config,
                db,
                store,
                gh,
                name="team",
                issue_ref="acme/app#1",
                member_specs=["coordinator@shell"],
                initiator="owner",
            )
        with pytest.raises(ValueError, match="separate checkout"):
            await teardown_swarm(
                config,
                db,
                store,
                {"name": "team", "worktree_dir": str(project / ".backbone/swarms/team")},
                status="done",
            )
        create.assert_not_called()
        remove.assert_not_called()
        stop.assert_not_called()
    assert not (project / ".backbone").exists()


async def test_separate_linked_metadata_refuses_before_reserving_unrelated_parent(owners, tmp_path):
    registry, (a, b) = owners
    repo = tmp_path / "linked"
    repo.mkdir()
    metadata = tmp_path / "metadata/alpha.git"
    private = metadata / "worktrees/linked"
    private.mkdir(parents=True)
    (private / "commondir").write_text("../..\n")
    (repo / ".git").write_text(f"gitdir: {private}\n")
    await install_project_ownership(a)
    with pytest.raises(ValueError, match="Cannot identify the main checkout"):
        claim_project_directories(a.reservation.data_dir, (repo,))
    # Refusal must not leave even a partial reservation for this attempt.
    registry.claim_projects(b.reservation.identity, b.reservation.data_dir, (repo, metadata.parent))


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_disabling_store_still_releases_owned_links(owners, tmp_path, db, runtime):
    _, (a, _) = owners
    project = repo_at(tmp_path / "project")
    spec = AgentSpec(name="worker", dir=str(project), runtime=runtime)
    store = AgentStore(db, a.reservation.data_dir)
    await store.start()
    await store.register(spec)
    source = a.reservation.data_dir / "skills/store/example"
    source.mkdir(parents=True)
    content = "---\nname: example\ndescription: Example\nmetadata:\n  backbone-tags: all\n---\n"
    (source / "SKILL.md").write_text(content)
    await store.set_setting("skills.store", str(source.parent))
    await install_project_ownership(a)
    rt = RUNTIMES[runtime]
    assert _materialize_skills(store.config, spec, rt)[1] is None
    link = project / rt.skill_dirs[0] / "example"
    assert link.is_symlink()
    await store.set_setting("skills.store", "")
    assert store.config.skills.store_path is None
    manifest = manifest_path(a.reservation.data_dir, "worker")
    original = manifest.read_text()
    malformed = json.loads(original)
    malformed["links"] = "not a list"
    manifest.write_text(json.dumps(malformed))
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        with pytest.raises(ValueError, match="Could not release"):
            await store.forget("worker")
        assert link.is_symlink() and json.loads(manifest.read_text()) == malformed
        assert store.agents.get("worker") is not None
        manifest.write_text(original)
        assert await store.forget("worker")
    assert not link.is_symlink()
    assert not manifest_path(a.reservation.data_dir, "worker").exists()
    assert (source / "SKILL.md").read_text() == content


async def test_swarm_refuses_separate_metadata_before_creating_a_worktree(owners, tmp_path, db):
    registry, (a, b) = owners
    project = tmp_path / "project"
    project.mkdir()
    metadata = tmp_path / "metadata/alpha.git"
    metadata.mkdir(parents=True)
    (project / ".git").write_text(f"gitdir: {metadata}\n")
    await install_project_ownership(a)
    config = bootstrap_config(a.reservation.data_dir)
    config = replace(
        config,
        agents=agents_from_rows([{"name": "owner", "dir": str(project), "repo": "acme/app"}]),
    )
    store = AgentStore(db, a.reservation.data_dir)
    await store.start()
    gh = AsyncMock()
    gh.get_issue.return_value = AsyncMock(state="open", title="Example")
    with (
        patch("agent_backbone.services.swarm.interface.session_exists", return_value=False),
        patch("agent_backbone.services.swarm.interface.is_git_repo", return_value=True),
        patch("agent_backbone.services.swarm.interface.current_branch", return_value="develop"),
        patch("agent_backbone.services.swarm.interface.create_worktree") as create,
    ):
        with pytest.raises(ValueError, match="Cannot identify the main checkout"):
            await create_swarm(
                config,
                db,
                store,
                gh,
                name="team",
                issue_ref="acme/app#1",
                member_specs=["coordinator@shell"],
                initiator="owner",
            )
        create.assert_not_called()
    assert await db.swarms.get("team") is None
    assert not list(store.agents)
    assert not (project / ".backbone").exists()
    registry.claim_projects(b.reservation.identity, b.reservation.data_dir, (project, metadata))


async def test_swarm_claims_separate_dot_git_main_before_creation(owners, tmp_path, db):
    registry, (a, b) = owners
    project = tmp_path / "project"
    project.mkdir()
    metadata = tmp_path / "metadata/.git"
    metadata.mkdir(parents=True)
    (project / ".git").write_text(f"gitdir: {metadata}\n")
    sibling = tmp_path / "metadata/other.git"
    sibling.mkdir()
    registry.claim_projects(b.reservation.identity, b.reservation.data_dir, (sibling,))
    await install_project_ownership(a)
    config = bootstrap_config(a.reservation.data_dir)
    config = replace(
        config,
        agents=agents_from_rows([{"name": "owner", "dir": str(project), "repo": "acme/app"}]),
    )
    store = AgentStore(db, a.reservation.data_dir)
    await store.start()
    gh = AsyncMock()
    gh.get_issue.return_value = AsyncMock(state="open", title="Example")
    with (
        patch("agent_backbone.services.swarm.interface.session_exists", return_value=False),
        patch("agent_backbone.services.swarm.interface.is_git_repo", return_value=True),
        patch("agent_backbone.services.swarm.interface.current_branch", return_value="develop"),
        patch("agent_backbone.services.swarm.interface.create_worktree") as create,
    ):
        with pytest.raises(ValueError, match="separate checkout"):
            await create_swarm(
                config,
                db,
                store,
                gh,
                name="team",
                issue_ref="acme/app#1",
                member_specs=["coordinator@shell"],
                initiator="owner",
            )
        create.assert_not_called()
    assert await db.swarms.get("team") is None
    assert not list(store.agents)
    registry.claim_projects(b.reservation.identity, b.reservation.data_dir, (project, metadata))
