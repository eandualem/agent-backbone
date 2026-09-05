"""Tests for the swarm layer: roster, briefs, lifecycle, and teardown."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.agents import AgentStore, StartResult
from agent_backbone.services.swarm import (
    SwarmError,
    create_swarm,
    parse_issue_ref,
    parse_member_spec,
    parse_roster,
    render_brief,
    teardown_for_issue,
    teardown_swarm,
)
from agent_backbone.services.swarm._roster import MemberSpec, member_names
from tests.conftest import make_config

_IFACE = "agent_backbone.services.swarm.interface"
_STARTED = StartResult(ok=True, ready="ready")
_FAILED = StartResult(ok=False)


class TestRoster:
    def test_full_spec(self):
        spec = parse_member_spec("scout*3@claude/sonnet")
        assert spec == MemberSpec(role="scout", count=3, runtime="claude", model="sonnet")

    def test_minimal_spec_defaults(self):
        assert parse_member_spec("reviewer") == MemberSpec(role="reviewer")

    def test_runtime_without_model(self):
        assert parse_member_spec("coder@codex") == MemberSpec(role="coder", runtime="codex")

    def test_model_may_be_a_provider_path(self):
        # OpenCode names models provider/model; the runtime is what follows "@".
        assert parse_member_spec("scout@opencode/google/gemini-3.8-flash") == MemberSpec(
            role="scout", runtime="opencode", model="google/gemini-3.8-flash"
        )

    def test_model_may_carry_an_effort(self):
        # The roster keeps the spec whole; the runtime splits it at launch.
        assert parse_member_spec("coordinator@codex/gpt-6-astra:high") == MemberSpec(
            role="coordinator", runtime="codex", model="gpt-6-astra:high"
        )

    def test_invalid_spec_rejected(self):
        with pytest.raises(ValueError):
            parse_member_spec("Scout One")

    def test_roster_defaults_a_coordinator(self):
        roster = parse_roster(["scout*2"])
        assert roster[0].role == "coordinator"
        assert roster[1] == MemberSpec(role="scout", count=2)

    def test_two_coordinators_rejected(self):
        with pytest.raises(ValueError):
            parse_roster(["coordinator", "coordinator@codex"])

    def test_member_names_numbered_only_when_plural(self):
        names = member_names("research", parse_roster(["scout*2", "coder"]))
        assert [n for n, _ in names] == [
            "research-coordinator",
            "research-scout-1",
            "research-scout-2",
            "research-coder",
        ]

    def test_same_role_across_specs_never_collides(self):
        # scout@codex + scout@claude used to both become "research-scout",
        # silently merging two members into one corrupted agent (found live).
        names = member_names(
            "research", parse_roster(["scout*2@codex", "scout@opencode", "scout@claude/sonnet"])
        )
        labels = [n for n, _ in names]
        assert labels == [
            "research-coordinator",
            "research-scout-1",
            "research-scout-2",
            "research-scout-3",
            "research-scout-4",
        ]
        assert len(set(labels)) == len(labels)
        assert names[3][1].runtime == "opencode"
        assert names[4][1].model == "sonnet"

    def test_role_named_like_a_numbered_member_is_rejected(self):
        # "scout*2" yields research-scout-1; a role literally called
        # "scout-1" would land on the same agent name and merge two members.
        with pytest.raises(ValueError, match="same agent name"):
            member_names("research", parse_roster(["scout*2", "scout-1"]))


class TestBriefs:
    def test_brief_renders_common_and_role_with_facts(self):
        facts = {
            "swarm_name": "research",
            "agent_name": "research-scout-1",
            "role": "scout",
            "coordinator": "research-coordinator",
            "initiator": "simon",
            "repo": "acme/app",
            "issue_number": "7",
            "issue_url": "https://github.com/acme/app/issues/7",
            "branch": "swarm/research",
            "base_branch": "v2",
            "worktree": "/x/.backbone/swarms/research",
            "members": "research-coordinator, research-scout-1",
        }
        brief = render_brief("scout", facts)
        assert "You are the agent **research-scout-1**" in brief
        assert "swarm/research" in brief
        assert "Your role: scout" in brief
        assert "{" not in brief.replace("{worktree}", "")  # placeholders all filled

    def test_unknown_role_falls_back_to_worker(self):
        brief = render_brief("cartographer", {"role": "cartographer"})
        assert "Your role: cartographer" in brief

    def test_coordinator_waits_for_kickoff(self):
        brief = render_brief("coordinator", {"swarm_name": "research"})
        assert "Before assigning work, wait for" in brief
        assert "[via:backbone swarm:research] Your\nswarm is live" in brief

    def test_data_dir_override_wins(self, tmp_path):
        override = tmp_path / "swarm-templates"
        override.mkdir()
        (override / "scout.md").write_text("custom scout for {swarm_name}")
        brief = render_brief("scout", {"swarm_name": "x"}, data_dir=tmp_path)
        assert "custom scout for x" in brief
        assert "Your role: scout" not in brief


class TestIssueRef:
    def test_parses(self):
        assert parse_issue_ref("acme/app#42") == ("acme/app", 42)

    def test_rejects_bare_number(self):
        with pytest.raises(SwarmError):
            parse_issue_ref("#42")


class _FakeStore:
    def __init__(self, config):
        self._config = config
        self.registered: list[AgentSpec] = []
        self.forgotten: list[str] = []

    @property
    def agents(self):
        specs = list(self._config.agents) + self.registered
        return _FakeAgents(specs)

    async def register(self, spec):
        self.registered.append(spec)
        return spec

    async def touch_started(self, name):
        pass

    async def forget(self, name):
        self.forgotten.append(name)
        self.registered = [s for s in self.registered if s.name != name]
        return True


class _FakeAgents:
    def __init__(self, specs):
        self._specs = specs

    def __iter__(self):
        return iter(self._specs)

    def get(self, name):
        return next((s for s in self._specs if s.name == name), None)


def _swarm_config(tmp_path):
    repo_dir = tmp_path / "checkout"
    repo_dir.mkdir()
    agents = AgentsConfig(
        specs={"simon": AgentSpec(name="simon", dir=str(repo_dir), repo="acme/app")}
    )
    return make_config(tmp_path, agents=agents), repo_dir


class TestCreateSwarm:
    @patch(f"{_IFACE}.safe_deliver", new_callable=AsyncMock, return_value=DeliveryOutcome.DELIVERED)
    @patch(f"{_IFACE}.start_agent", new_callable=AsyncMock, return_value=_STARTED)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.create_worktree", new_callable=AsyncMock)
    @patch(f"{_IFACE}.current_branch", new_callable=AsyncMock, return_value="main")
    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    async def test_create_full_flow(
        self, _git, _branch, mock_wt, _exists, mock_start, mock_deliver, db, tmp_path
    ):
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        mock_wt.return_value = (worktree, "swarm/research")
        store = _FakeStore(config)
        gh = AsyncMock()
        gh.get_issue = AsyncMock(return_value=AsyncMock(state="open", title="Do the research"))

        async def start_with_registered_roster(*args, **kwargs):
            assert {s.name for s in store.registered} == {
                "research-coordinator",
                "research-scout-1",
                "research-scout-2",
            }
            return _STARTED

        mock_start.side_effect = start_with_registered_roster

        result = await create_swarm(
            config,
            db,
            store,
            gh,
            name="research",
            issue_ref="acme/app#7",
            member_specs=["scout*2@claude/sonnet"],
            initiator="simon",
        )

        assert result.coordinator == "research-coordinator"
        assert result.members == [
            "research-coordinator",
            "research-scout-1",
            "research-scout-2",
        ]
        # Members registered in the shared worktree with swarm tags.
        assert all(s.dir == str(worktree) for s in store.registered)
        assert "swarm:research" in store.registered[0].tags
        assert [call.args[0].name for call in mock_start.await_args_list] == [
            "research-scout-1",
            "research-scout-2",
            "research-coordinator",
        ]
        # Every member is started with its role brief.
        launch = mock_start.await_args_list[-1].kwargs
        assert launch["brief_file"] is not None
        brief = Path(launch["brief_file"]).read_text()
        assert "research-coordinator" in brief and "acme/app" in brief
        # Kickoff went to the coordinator.
        assert mock_deliver.await_args.args[0] == "research-coordinator"
        assert "Do the research" in mock_deliver.await_args.args[1]
        # Recorded as active.
        row = await db.swarms.get("research")
        assert row["status"] == "active" and row["issue_number"] == 7
        # Whether a member asks is start_agent's call at each launch (from
        # `swarm.unattended_members` and the runtime's sandbox), never stored.
        assert not any(s.unattended for s in store.registered)

    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    async def test_closed_issue_rejected(self, _exists, _git, db, tmp_path):
        config, _ = _swarm_config(tmp_path)
        gh = AsyncMock()
        gh.get_issue = AsyncMock(return_value=AsyncMock(state="closed", title="t"))
        with pytest.raises(SwarmError, match="closed"):
            await create_swarm(
                config,
                db,
                _FakeStore(config),
                gh,
                name="research",
                issue_ref="acme/app#7",
                member_specs=[],
                initiator="simon",
            )

    @patch(f"{_IFACE}.remove_worktree", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.safe_deliver", new_callable=AsyncMock, return_value=DeliveryOutcome.DELIVERED)
    @patch(f"{_IFACE}.start_agent", new_callable=AsyncMock)
    @patch(f"{_IFACE}.stop_session", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.create_worktree", new_callable=AsyncMock)
    @patch(f"{_IFACE}.current_branch", new_callable=AsyncMock, return_value="main")
    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    @pytest.mark.parametrize("occupied", [False, True])
    async def test_failed_member_start_rolls_back(
        self,
        _git,
        _branch,
        mock_wt,
        _exists,
        mock_stop,
        _start,
        _deliver,
        mock_rm,
        db,
        tmp_path,
        occupied,
    ):
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        mock_wt.return_value = (worktree, "swarm/research")
        store = _FakeStore(config)
        gh = AsyncMock()
        gh.get_issue = AsyncMock(return_value=AsyncMock(state="open", title="t"))

        _start.side_effect = [
            _STARTED,
            StartResult(ok=True, already_running=True) if occupied else _FAILED,
        ]
        with pytest.raises(SwarmError, match="became occupied" if occupied else "failed to start"):
            await create_swarm(
                config,
                db,
                store,
                gh,
                name="research",
                issue_ref="acme/app#7",
                member_specs=["scout"],
                initiator="simon",
            )

        mock_stop.assert_awaited_once_with("research-scout")
        _deliver.assert_not_awaited()
        if occupied:
            mock_rm.assert_not_awaited()
            assert [agent.name for agent in store.registered] == ["research-coordinator"]
        else:
            mock_rm.assert_awaited_once()
            assert store.registered == []  # all rolled back
        assert (await db.swarms.get("research"))["status"] == (
            "active" if occupied else "disbanded"
        )

    @patch(f"{_IFACE}.safe_deliver", new_callable=AsyncMock, return_value=DeliveryOutcome.DELIVERED)
    @patch(f"{_IFACE}.start_agent", new_callable=AsyncMock, return_value=_STARTED)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.create_worktree", new_callable=AsyncMock)
    @patch(f"{_IFACE}.current_branch", new_callable=AsyncMock, return_value="main")
    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    async def test_each_member_gets_its_own_role_brief(
        self, _git, _branch, mock_wt, _exists, mock_start, mock_deliver, db, tmp_path
    ):
        # start_agent decides launch injection vs first message per runtime;
        # the swarm only hands every member its role brief.
        config, repo_dir = _swarm_config(tmp_path)
        mock_wt.return_value = (repo_dir / ".backbone" / "swarms" / "research", "swarm/research")
        gh = AsyncMock()
        gh.get_issue = AsyncMock(return_value=AsyncMock(state="open", title="t"))

        await create_swarm(
            config,
            db,
            _FakeStore(config),
            gh,
            name="research",
            issue_ref="acme/app#7",
            member_specs=["scout@aider", "probe@shell"],
            initiator="simon",
        )

        briefs = {
            c.args[0].name: Path(c.kwargs["brief_file"]).name for c in mock_start.await_args_list
        }
        assert briefs == {
            "research-coordinator": "research-coordinator.md",
            "research-scout": "research-scout.md",
            "research-probe": "research-probe.md",
        }
        # Only the kickoff goes through delivery here.
        assert [c.kwargs["source"] for c in mock_deliver.await_args_list] == ["swarm-kickoff"]

    @patch(f"{_IFACE}.remove_worktree", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.start_agent", new_callable=AsyncMock, return_value=_STARTED)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.create_worktree", new_callable=AsyncMock)
    @patch(f"{_IFACE}.current_branch", new_callable=AsyncMock, return_value="main")
    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    async def test_lost_registration_race_removes_the_worktree(
        self, _git, _branch, mock_wt, _exists, mock_start, mock_rm, db, tmp_path
    ):
        # A concurrent swarm can take the issue between the pre-check and the
        # insert (uq_swarms_active_issue); the fresh worktree must not linger.
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        mock_wt.return_value = (worktree, "swarm/research")
        gh = AsyncMock()
        gh.get_issue = AsyncMock(return_value=AsyncMock(state="open", title="t"))

        with (
            patch.object(db.swarms, "create", AsyncMock(side_effect=RuntimeError("UNIQUE"))),
            pytest.raises(SwarmError, match="could not register"),
        ):
            await create_swarm(
                config,
                db,
                _FakeStore(config),
                gh,
                name="research",
                issue_ref="acme/app#7",
                member_specs=["scout"],
                initiator="simon",
            )

        mock_rm.assert_awaited_once_with(repo_dir, worktree)
        mock_start.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", ["remove_false", "remove_error", "status_error", "forget_error"]
)
async def test_incomplete_startup_rollback_remains_retryable(db, tmp_path, failure):
    config, repo_dir = _swarm_config(tmp_path)
    worktree = repo_dir / ".backbone" / "swarms" / "research"
    store = _FakeStore(config)
    gh = AsyncMock()
    gh.get_issue.return_value = AsyncMock(state="open", title="task")
    if failure == "forget_error":
        store.forget = AsyncMock(side_effect=OSError("forget failed"))
    status = db.swarms.set_status
    with (
        patch(f"{_IFACE}.is_git_repo", AsyncMock(return_value=True)),
        patch(f"{_IFACE}.current_branch", AsyncMock(return_value="develop")),
        patch(f"{_IFACE}.create_worktree", AsyncMock(return_value=(worktree, "swarm/research"))),
        patch(f"{_IFACE}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_IFACE}.start_agent", AsyncMock(return_value=_FAILED)),
        patch(
            f"{_IFACE}.remove_worktree",
            AsyncMock(
                return_value=failure != "remove_false",
                side_effect=OSError("remove failed") if failure == "remove_error" else None,
            ),
        ),
        patch.object(
            db.swarms,
            "set_status",
            AsyncMock(
                side_effect=OSError("status failed") if failure == "status_error" else status,
            ),
        ),
        pytest.raises(SwarmError, match="failed to start member"),
    ):
        await create_swarm(
            config,
            db,
            store,
            gh,
            name="research",
            issue_ref="acme/app#7",
            member_specs=["scout"],
            initiator="simon",
        )
    assert (await db.swarms.get("research"))["status"] == "active"


class TestTeardown:
    async def test_teardown_holds_member_lock_through_removal_and_forget(self, db, tmp_path):
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        store = AgentStore(db, tmp_path)
        await store.start()
        await store.register(
            AgentSpec(name="research-scout", dir=str(worktree), tags=("swarm:research",))
        )
        removing, release = asyncio.Event(), asyncio.Event()

        async def remove(*args):
            removing.set()
            await release.wait()
            return True

        with (
            patch(f"{_IFACE}.session_exists", AsyncMock(return_value=False)),
            patch(f"{_IFACE}.remove_worktree", side_effect=remove),
        ):
            teardown = asyncio.create_task(
                teardown_swarm(
                    config,
                    db,
                    store,
                    {"name": "research", "worktree_dir": str(worktree)},
                    status="done",
                )
            )
            await asyncio.wait_for(removing.wait(), 2)
            update = asyncio.create_task(store.update("research-scout", model="late"))
            await asyncio.sleep(0)
            assert not update.done()
            release.set()
            assert await asyncio.wait_for(teardown, 2) == ["research-scout"]
            with pytest.raises(KeyError):
                await asyncio.wait_for(update, 2)
        assert store.agents.get("research-scout") is None

    @patch(f"{_IFACE}.remove_worktree", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.stop_session", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=True)
    async def test_issue_close_tears_down_the_swarm(
        self, _exists, mock_stop, mock_rm, db, tmp_path
    ):
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        await db.swarms.create(
            "research",
            repo="acme/app",
            issue_number=7,
            initiator="simon",
            coordinator="research-coordinator",
            branch="swarm/research",
            worktree_dir=str(worktree),
        )
        store = _FakeStore(config)
        store.registered = [
            AgentSpec(
                name="research-coordinator",
                dir=str(worktree),
                tags=("swarm:research", "role:coordinator"),
            ),
            AgentSpec(
                name="research-scout-1",
                dir=str(worktree),
                tags=("swarm:research", "role:scout"),
            ),
        ]

        name = await teardown_for_issue(config, db, store, "acme/app", 7)

        assert name == "research"
        assert mock_stop.await_count == 2
        assert store.forgotten == ["research-coordinator", "research-scout-1"]
        assert (await db.swarms.get("research"))["status"] == "done"

    @patch(f"{_IFACE}.remove_worktree", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.stop_session", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    async def test_a_repository_that_is_gone_leaves_nothing_to_remove(
        self, _exists, _stop, remove_worktree, db, tmp_path
    ):
        """The checkout was deleted: git cannot be asked, and teardown must not
        wedge the swarm for good."""
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        await db.swarms.create(
            "research",
            repo="acme/app",
            issue_number=7,
            initiator="simon",
            coordinator="research-coordinator",
            branch="swarm/research",
            worktree_dir=str(worktree),
        )
        store = _FakeStore(config)
        store.registered = [
            AgentSpec(
                name="research-coordinator",
                dir=str(worktree),
                tags=("swarm:research", "role:coordinator"),
            )
        ]
        shutil.rmtree(repo_dir)

        assert await teardown_for_issue(config, db, store, "acme/app", 7) == "research"
        remove_worktree.assert_not_awaited()
        assert store.forgotten == ["research-coordinator"]
        assert (await db.swarms.get("research"))["status"] == "done"

    @patch(f"{_IFACE}.remove_worktree", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.stop_session", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=True)
    async def test_failed_member_stop_preserves_worktree_and_registration(
        self, _exists, _stop, remove_worktree, db, tmp_path
    ):
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        await db.swarms.create(
            "research",
            repo="acme/app",
            issue_number=7,
            initiator="simon",
            coordinator="research-coordinator",
            branch="swarm/research",
            worktree_dir=str(worktree),
        )
        store = _FakeStore(config)
        store.registered = [
            AgentSpec(
                name="research-coordinator",
                dir=str(worktree),
                tags=("swarm:research", "role:coordinator"),
            )
        ]

        with pytest.raises(SwarmError, match="could not stop swarm member"):
            await teardown_for_issue(config, db, store, "acme/app", 7)

        remove_worktree.assert_not_awaited()
        assert store.forgotten == []
        assert (await db.swarms.get("research"))["status"] == "active"

    async def test_no_swarm_for_issue_is_none(self, db, tmp_path):
        config, _ = _swarm_config(tmp_path)
        assert await teardown_for_issue(config, db, _FakeStore(config), "acme/app", 99) is None


class TestOwnRepoGuardrail:
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    async def test_agent_cannot_swarm_on_foreign_repo(self, _git, _exists, db, tmp_path):
        """An agent initiator must own the issue's repository."""
        config, _ = _swarm_config(tmp_path)  # simon owns acme/app
        gh = AsyncMock()
        gh.get_issue = AsyncMock(return_value=AsyncMock(state="open", title="t"))
        with pytest.raises(SwarmError, match="own repository"):
            await create_swarm(
                config,
                db,
                _FakeStore(config),
                gh,
                name="foreign",
                issue_ref="acme/other#5",
                member_specs=[],
                initiator="simon",
            )
