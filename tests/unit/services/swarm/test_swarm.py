"""Tests for the swarm layer: roster, briefs, lifecycle, and teardown."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.services.database import BackboneDB, build_engine
from agent_backbone.services.swarm import (
    SwarmError,
    create_swarm,
    parse_issue_ref,
    parse_member_spec,
    parse_roster,
    render_brief,
    teardown_for_issue,
)
from agent_backbone.services.swarm._roster import MemberSpec, member_names
from tests.conftest import make_config

_IFACE = "agent_backbone.services.swarm.interface"


class TestRoster:
    def test_full_spec(self):
        spec = parse_member_spec("scout*3@claude/sonnet")
        assert spec == MemberSpec(role="scout", count=3, runtime="claude", model="sonnet")

    def test_minimal_spec_defaults(self):
        assert parse_member_spec("reviewer") == MemberSpec(role="reviewer")

    def test_runtime_without_model(self):
        assert parse_member_spec("coder@codex") == MemberSpec(role="coder", runtime="codex")

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


@pytest.fixture
async def db():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    db = BackboneDB(engine)
    await db.start()
    try:
        yield db
    finally:
        db._engine = None
        await engine.dispose()


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
    @patch(f"{_IFACE}.safe_deliver", new_callable=AsyncMock, return_value="delivered")
    @patch(f"{_IFACE}.wait_until_ready", new_callable=AsyncMock, return_value=("ready", []))
    @patch(f"{_IFACE}.start_agent", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.create_worktree", new_callable=AsyncMock)
    @patch(f"{_IFACE}.current_branch", new_callable=AsyncMock, return_value="main")
    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    async def test_create_full_flow(
        self, _git, _branch, mock_wt, _exists, mock_start, _ready, mock_deliver, db, tmp_path
    ):
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        mock_wt.return_value = (worktree, "swarm/research")
        store = _FakeStore(config)
        gh = AsyncMock()
        gh.get_issue = AsyncMock(return_value=AsyncMock(state="open", title="Do the research"))

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
        # Claude members get their brief as a system prompt file.
        launch = mock_start.await_args_list[0].kwargs
        assert launch["system_prompt_file"] is not None
        brief = Path(launch["system_prompt_file"]).read_text()
        assert "research-coordinator" in brief and "acme/app" in brief
        # Kickoff went to the coordinator.
        assert mock_deliver.await_args.args[0] == "research-coordinator"
        assert "Do the research" in mock_deliver.await_args.args[1]
        # Recorded as active.
        row = await db.get_swarm("research")
        assert row["status"] == "active" and row["issue_number"] == 7

    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    async def test_closed_issue_rejected(self, _git, db, tmp_path):
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
    @patch(f"{_IFACE}.safe_deliver", new_callable=AsyncMock, return_value="delivered")
    @patch(f"{_IFACE}.wait_until_ready", new_callable=AsyncMock, return_value=("ready", []))
    @patch(f"{_IFACE}.start_agent", new_callable=AsyncMock, side_effect=[True, False])
    @patch(f"{_IFACE}.stop_session", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=False)
    @patch(f"{_IFACE}.create_worktree", new_callable=AsyncMock)
    @patch(f"{_IFACE}.current_branch", new_callable=AsyncMock, return_value="main")
    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    async def test_failed_member_start_rolls_back(
        self,
        _git,
        _branch,
        mock_wt,
        _exists,
        mock_stop,
        _start,
        _ready,
        _deliver,
        mock_rm,
        db,
        tmp_path,
    ):
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        mock_wt.return_value = (worktree, "swarm/research")
        store = _FakeStore(config)
        gh = AsyncMock()
        gh.get_issue = AsyncMock(return_value=AsyncMock(state="open", title="t"))

        with pytest.raises(SwarmError, match="failed to start"):
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

        mock_rm.assert_awaited_once()
        assert store.registered == []  # all rolled back
        assert (await db.get_swarm("research"))["status"] == "disbanded"


class TestTeardown:
    @patch(f"{_IFACE}.remove_worktree", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.stop_session", new_callable=AsyncMock, return_value=True)
    @patch(f"{_IFACE}.session_exists", new_callable=AsyncMock, return_value=True)
    async def test_issue_close_tears_down_the_swarm(
        self, _exists, mock_stop, mock_rm, db, tmp_path
    ):
        config, repo_dir = _swarm_config(tmp_path)
        worktree = repo_dir / ".backbone" / "swarms" / "research"
        await db.create_swarm(
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
        assert (await db.get_swarm("research"))["status"] == "done"

    async def test_no_swarm_for_issue_is_none(self, db, tmp_path):
        config, _ = _swarm_config(tmp_path)
        assert await teardown_for_issue(config, db, _FakeStore(config), "acme/app", 99) is None


class TestOwnRepoGuardrail:
    @patch(f"{_IFACE}.is_git_repo", new_callable=AsyncMock, return_value=True)
    async def test_agent_cannot_swarm_on_foreign_repo(self, _git, db, tmp_path):
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
