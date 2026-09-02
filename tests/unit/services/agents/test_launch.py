"""Tests for starting, waiting on and answering agent sessions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.config import AgentSpec, bootstrap_config
from agent_backbone.services.agents import (
    approve_agent,
    read_state_file,
    start_agent,
    wait_until_ready,
    write_state_file,
)
from agent_backbone.services.agents.models import AgentState

_MOD = "agent_backbone.services.agents.launch"
_BASE = "agent_backbone.services.runtimes.base"


class TestApproveAgent:
    DIALOG = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
    IDLE = "❯ \n  ? for shortcuts\n"

    async def test_answers_only_a_visible_prompt(self):
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", side_effect=[self.DIALOG, self.IDLE]),
            patch(f"{_BASE}.send_keys", return_value=True) as keys,
        ):
            outcome, evidence = await approve_agent("ike", runtime="claude", settle_seconds=0)
        assert outcome == "approved"
        assert keys.await_args.args == ("ike", "Enter")
        assert evidence[0] == "answered with Enter; prompt cleared"
        assert "Do you want to proceed?" in evidence  # the dialog is quoted for the audit

    async def test_idle_prompt_is_never_typed_into(self):
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value=self.IDLE),
            patch(f"{_BASE}.send_keys") as keys,
        ):
            outcome, evidence = await approve_agent("ike", runtime="claude", settle_seconds=0)
        assert outcome == "not_waiting"
        assert evidence[0].startswith("terminal shows no active permission prompt")
        keys.assert_not_called()

    async def test_unknown_answer_sequence_is_refused(self):
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value="$ "),
            patch(f"{_BASE}.send_keys") as keys,
        ):
            outcome, _ = await approve_agent("ike", runtime="shell", settle_seconds=0)
        assert outcome == "unsupported"
        keys.assert_not_awaited()

    async def test_offline(self):
        with patch(f"{_MOD}.session_exists", return_value=False):
            assert (await approve_agent("ike", runtime="claude"))[0] == "offline"

    async def test_stale_dialog_above_an_idle_prompt_is_not_answered(self):
        # The dialog text is still on screen but the runtime is back at its
        # prompt with typed text — Enter would submit that text.
        stale = self.DIALOG + "❯ rm -rf build\n  ? for shortcuts\n"
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value=stale),
            patch(f"{_BASE}.send_keys") as keys,
        ):
            outcome, _ = await approve_agent("ike", runtime="claude", settle_seconds=0)
        assert outcome == "not_waiting"
        keys.assert_not_called()


class TestStartAgentScrubsSecrets:
    """A started agent inherits the launch contract and nothing else (issue #81)."""

    async def test_secrets_are_scrubbed_from_the_session(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / ".env").write_text("BACKBONE_API_KEY=k\nMY_OWN_SECRET=s\n")
        project = tmp_path / "project"
        project.mkdir()
        spec = AgentSpec(name="ike", dir=str(project), runtime="shell")

        config = bootstrap_config(data_dir)
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
        ):
            assert (await start_agent(spec, config, wait=False)).ok is True

        scrub = start.await_args.kwargs["scrub"]
        assert "BACKBONE_API_KEY" in scrub
        assert "GITHUB_TOKEN" in scrub  # a known name, even absent from .env
        assert "MY_OWN_SECRET" in scrub  # whatever the user put in .env
        # The launch contract itself is untouched.
        assert start.await_args.kwargs["environment"]["BACKBONE_AGENT"] == "ike"


class TestStartAgentBrief:
    """One launch path: the brief reaches every runtime, at launch or as a message."""

    @staticmethod
    def _spec(tmp_path, runtime):
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        return AgentSpec(name="ike", dir=str(project), runtime=runtime, repo="acme/app")

    @staticmethod
    def _launch():
        return (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True),
            patch(f"{_BASE}.resolve_command", return_value="/bin/x"),
            patch(f"{_BASE}.Runtime.pre_trust"),
            patch(f"{_MOD}.wait_until_ready", new_callable=AsyncMock, return_value=("ready", [])),
        )

    async def test_claude_gets_the_brief_at_launch(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        db = AsyncMock()
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            result = await start_agent(self._spec(tmp_path, "claude"), config, db=db)
        assert result.ok and result.ready == "ready"
        command = started.await_args.kwargs["command"]
        assert "--append-system-prompt-file" in command
        db.enqueue_message.assert_not_awaited()

    async def test_aider_gets_the_brief_queued_as_its_first_message(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        db = AsyncMock()
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            result = await start_agent(self._spec(tmp_path, "aider"), config, db=db)
        assert result.ok
        assert "--append-system-prompt-file" not in started.await_args.kwargs["command"]
        db.enqueue_message.assert_awaited_once()
        queued = db.enqueue_message.await_args.kwargs
        assert queued["session_name"] == "ike"
        assert queued["message"].startswith("[via:backbone] ")
        assert queued["delivery_kind"] == "direct_message"
        assert queued["source"] == "agent-brief"

    async def test_a_swarm_role_brief_replaces_the_common_brief(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        db = AsyncMock()
        role = tmp_path / "role.md"
        role.write_text("You are the scout.")
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start, _cmd, _trust, _wait:
            await start_agent(self._spec(tmp_path, "aider"), config, brief_file=role, db=db)
        assert (
            db.enqueue_message.await_args.kwargs["message"] == "[via:backbone] You are the scout."
        )

    async def test_shell_and_resume_get_no_brief(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        db = AsyncMock()
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start, _cmd, _trust, _wait:
            await start_agent(self._spec(tmp_path, "shell"), config, db=db)
            await start_agent(self._spec(tmp_path, "aider"), config, resume=True, db=db)
        db.enqueue_message.assert_not_awaited()

    async def test_unknown_runtime_is_refused(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        with patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False):
            result = await start_agent(self._spec(tmp_path, "cursor"), config)
        assert result.ok is False
        assert result.evidence == ("unknown runtime: cursor",)


class TestStartingState:
    """`starting` is written at launch and gives way to the real state."""

    async def test_start_agent_writes_starting_then_wait_clears_it(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        project = tmp_path / "project"
        project.mkdir()
        spec = AgentSpec(name="ike", dir=str(project), runtime="shell")
        seen: list[str] = []

        async def _capture(name, lines=60):
            # By the time the terminal is read, the marker must be on disk.
            seen.append(read_state_file(config.state_dir, name).state.value)
            return "$ "

        with (
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True),
            patch(f"{_MOD}.capture_pane", side_effect=_capture),
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, side_effect=[False, True]),
        ):
            result = await start_agent(spec, config)

        assert result.ready == "ready"
        assert seen == [AgentState.STARTING.value]
        # The prompt showed: the marker is gone and the terminal decides again.
        assert read_state_file(config.state_dir, "ike") is None

    async def test_hook_state_newer_than_the_launch_wins(self, tmp_path):
        state_dir = tmp_path / "state"
        launched = 1_000.0
        write_state_file(state_dir, "ike", {"state": "idle", "ts": launched + 2})
        with patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True):
            outcome, evidence = await wait_until_ready(
                "ike", state_dir=state_dir, runtime="claude", timeout=1, since=launched
            )
        assert outcome == "ready" and evidence[0].startswith("hook reported idle")
