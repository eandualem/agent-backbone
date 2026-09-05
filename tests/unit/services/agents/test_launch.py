"""Tests for starting, waiting on and answering agent sessions."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec, bootstrap_config
from agent_backbone.services.agents import (
    approve_agent,
    deny_agent,
    plan_control,
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

    MODEL_SWITCH = (
        "  Approaching rate limits\n"
        "  Switch to gpt-5.6-luna for lower credit usage?\n"
        "› 1. Switch to gpt-5.6-luna\n"
        "  2. Keep current model\n"
        "  3. Keep current model (never show again)\n"
        "  Press enter to confirm or esc to go back\n"
    )

    async def test_a_choice_dialog_is_not_approved(self):
        # Enter on Codex's rate-limit dialog switches the model; nothing is typed.
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value=self.MODEL_SWITCH),
            patch(f"{_BASE}.send_keys") as keys,
        ):
            outcome, evidence = await approve_agent("ike", runtime="codex", settle_seconds=0)
        assert outcome == "not_permission"
        assert "choice, not a permission prompt" in evidence[0]
        keys.assert_not_called()

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


class TestDenyAgent:
    DIALOG = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel\n"
    IDLE = "❯ \n  ? for shortcuts\n"

    async def test_refuses_only_a_visible_prompt(self):
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True),
            patch(f"{_MOD}.capture_pane", side_effect=[self.DIALOG, self.IDLE]),
            patch(f"{_BASE}.send_keys", new_callable=AsyncMock, return_value=True) as keys,
        ):
            outcome, evidence = await deny_agent("ike", runtime="claude", settle_seconds=0)
        assert outcome == "denied"
        keys.assert_awaited_once_with("ike", "Escape")
        assert evidence[0].startswith("sent Escape to claude; dialog cleared")

    async def test_idle_prompt_is_never_typed_into(self):
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True),
            patch(f"{_MOD}.capture_pane", return_value=self.IDLE),
            patch(f"{_BASE}.send_keys", new_callable=AsyncMock) as keys,
        ):
            outcome, _ = await deny_agent("ike", runtime="claude")
        assert outcome == "not_waiting"
        keys.assert_not_called()

    async def test_runtimes_without_a_verified_key_are_refused(self):
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True),
            patch(
                f"{_MOD}.capture_pane", return_value="│ Allow execution?\n│ ● 1. Yes, allow once\n"
            ),
            patch(f"{_BASE}.send_keys", new_callable=AsyncMock) as keys,
        ):
            outcome, _ = await deny_agent("ike", runtime="gemini")
        assert outcome == "unsupported"
        keys.assert_not_called()

    """Plan approve/reject go through the runtime's own keys, or nowhere."""

    async def test_claude_approve_sends_shift_tab(self):
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value="❯ \n"),
            patch(f"{_BASE}.send_keys", return_value=True) as keys,
        ):
            outcome, evidence = await plan_control("ike", "approve", runtime="claude")
        assert outcome == "approved"
        assert [c.args for c in keys.await_args_list] == [("ike", "Escape"), ("ike", "[Z")]
        assert evidence == ["sent Escape [Z to claude"]

    async def test_claude_reject_only_leaves_plan_mode(self):
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value="❯ \n"),
            patch(f"{_BASE}.send_keys", return_value=True) as keys,
        ):
            outcome, _ = await plan_control("ike", "reject", runtime="claude")
        assert outcome == "rejected"
        assert [c.args for c in keys.await_args_list] == [("ike", "Escape")]

    @pytest.mark.parametrize(
        "runtime", ["codex", "opencode", "gemini", "deepcode", "aider", "shell"]
    )
    async def test_other_runtimes_get_no_keys_at_all(self, runtime):
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value="› \n"),
            patch(f"{_BASE}.send_keys") as keys,
        ):
            outcome, evidence = await plan_control("ike", "approve", runtime=runtime)
        assert outcome == "unsupported"
        assert "nothing was sent" in evidence[0]
        keys.assert_not_called()

    async def test_partial_sequence_is_reported_not_hidden(self):
        # Escape goes in, "[Z" is refused: plan mode was left without approval.
        with (
            patch(f"{_MOD}.session_exists", return_value=True),
            patch(f"{_MOD}.capture_pane", return_value="❯ \n"),
            patch(f"{_BASE}.send_keys", side_effect=[True, False]),
        ):
            outcome, evidence = await plan_control("ike", "approve", runtime="claude")
        assert outcome == "failed"
        assert "sent Escape but tmux refused [Z" in evidence[0]
        assert "may have left plan mode" in evidence[0]

    async def test_offline_and_bad_action(self):
        with patch(f"{_MOD}.session_exists", return_value=False):
            assert (await plan_control("ike", "approve", runtime="claude"))[0] == "offline"
        with pytest.raises(ValueError):
            await plan_control("ike", "respond", runtime="claude")


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
        db.queue.enqueue.assert_not_awaited()

    async def test_aider_gets_the_brief_queued_as_its_first_message(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        db = AsyncMock()
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            result = await start_agent(self._spec(tmp_path, "aider"), config, db=db)
        assert result.ok
        assert "--append-system-prompt-file" not in started.await_args.kwargs["command"]
        db.queue.enqueue.assert_awaited_once()
        queued = db.queue.enqueue.await_args.kwargs
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
        assert db.queue.enqueue.await_args.kwargs["message"] == "[via:backbone] You are the scout."

    async def test_shell_and_resume_get_no_brief(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        db = AsyncMock()
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start, _cmd, _trust, _wait:
            await start_agent(self._spec(tmp_path, "shell"), config, db=db)
            await start_agent(self._spec(tmp_path, "aider"), config, resume=True, db=db)
        db.queue.enqueue.assert_not_awaited()

    async def test_unknown_runtime_is_refused(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        with patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False):
            result = await start_agent(self._spec(tmp_path, "cursor"), config)
        assert result.ok is False
        assert result.evidence == ("unknown runtime: cursor",)

    async def test_an_unattended_agent_gets_its_runtimes_switch_and_writable_dirs(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        config = replace(config, launch=replace(config.launch, writable_dirs=("/cache",)))
        spec = replace(self._spec(tmp_path, "codex"), unattended=True)
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            result = await start_agent(spec, config, db=AsyncMock())
        assert result.ok
        command = started.await_args.kwargs["command"]
        assert command[1:7] == ["-a", "never", "-s", "workspace-write", "--add-dir", "/cache"]

    async def test_a_worktree_member_can_reach_its_shared_git_dir(self, tmp_path):
        # A swarm worktree's index and refs live under <main>/.git — outside
        # the sandbox's writable root — so that directory is opened for it.
        config = bootstrap_config(tmp_path / "data")
        main_git = tmp_path / "main" / ".git"
        (main_git / "worktrees" / "wt").mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {main_git / 'worktrees' / 'wt'}\n")
        spec = AgentSpec(name="ike", dir=str(worktree), runtime="codex", repo="acme/app")
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            assert (await start_agent(spec, config, db=AsyncMock())).ok
        command = started.await_args.kwargs["command"]
        assert command[1:3] == ["--add-dir", str(main_git)]
        # A directory with no Git metadata has nothing to open.
        plain = self._spec(tmp_path, "codex")
        with exists, start as started, _cmd, _trust, _wait:
            assert (await start_agent(plain, config, db=AsyncMock())).ok
        assert "--add-dir" not in started.await_args.kwargs["command"]

    @pytest.mark.parametrize("unattended", [False, True])
    async def test_a_plain_checkout_explicitly_opens_git_metadata(self, tmp_path, unattended):
        config = bootstrap_config(tmp_path / "data")
        spec = replace(self._spec(tmp_path, "codex"), unattended=unattended)
        (spec.path / ".git").mkdir()
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            assert (await start_agent(spec, config, db=AsyncMock())).ok
        command = started.await_args.kwargs["command"]
        grant = command.index("--add-dir")
        assert command[grant + 1] == str((spec.path / ".git").resolve())

    @pytest.mark.parametrize("runtime", ["codex", "claude", "shell"])
    async def test_mouse_scrolling_follows_the_runtime(self, tmp_path, runtime):
        config = bootstrap_config(tmp_path / "data")
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            assert (await start_agent(self._spec(tmp_path, runtime), config, db=AsyncMock())).ok
        assert started.await_args.kwargs["mouse"] is (runtime == "codex")

    async def test_auto_review_setting_reaches_the_runtime(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        config = replace(config, launch=replace(config.launch, auto_review=True))
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            assert (await start_agent(self._spec(tmp_path, "codex"), config, db=AsyncMock())).ok
        assert "--approve-for-me" in started.await_args.kwargs["command"]

    @pytest.mark.parametrize(
        ("runtime", "setting", "never_asks"),
        [("codex", True, True), ("codex", False, False), ("opencode", True, False)],
    )
    async def test_a_swarm_member_never_asks_only_behind_a_sandbox(
        self, tmp_path, runtime, setting, never_asks
    ):
        # Decided at launch from the setting and the runtime — nothing stored
        # on the member, so the rule follows setting flips and runtime changes.
        from agent_backbone.config import SwarmConfig

        config = bootstrap_config(tmp_path / "data")
        config = replace(config, swarm=SwarmConfig(unattended_members=setting))
        spec = replace(self._spec(tmp_path, runtime), tags=("swarm:audit", "role:scout"))
        assert spec.swarm == "audit" and not spec.unattended
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            assert (await start_agent(spec, config, db=AsyncMock())).ok
        command = started.await_args.kwargs["command"]
        assert ("never" in command) is never_asks
        assert "--auto" not in command  # OpenCode's switch is never the swarm's call

    async def test_unattended_is_refused_for_a_runtime_without_a_switch(self, tmp_path):
        # Refused, not launched attended: it would park on its first dialog.
        config = bootstrap_config(tmp_path / "data")
        spec = replace(self._spec(tmp_path, "aider"), unattended=True)
        exists, start, _cmd, _trust, _wait = self._launch()
        with exists, start as started, _cmd, _trust, _wait:
            result = await start_agent(spec, config, db=AsyncMock())
        assert result.ok is False
        assert "no unattended switch" in result.evidence[0]
        started.assert_not_awaited()


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
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True),
            patch(f"{_MOD}.capture_pane", new_callable=AsyncMock, return_value="❯ \n"),
        ):
            outcome, evidence = await wait_until_ready(
                "ike", state_dir=state_dir, runtime="claude", timeout=1, since=launched
            )
        assert outcome == "ready" and evidence[0].startswith("hook reported idle")

    async def test_a_dialog_on_screen_beats_the_hooks_idle(self, tmp_path):
        """`claude --resume` fires SessionStart with its picker still up; start
        must report the question, not `ready`."""
        state_dir = tmp_path / "state"
        launched = 1_000.0
        write_state_file(state_dir, "ike", {"state": "idle", "ts": launched + 2})
        picker = (
            "  ❯ 1. Resume from summary (recommended)\n"
            "    2. Resume full session as-is\n"
            "  Enter to confirm · Esc to cancel\n"
        )
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True),
            patch(f"{_MOD}.capture_pane", new_callable=AsyncMock, return_value=picker),
        ):
            outcome, evidence = await wait_until_ready(
                "ike", state_dir=state_dir, runtime="claude", timeout=1, since=launched
            )
        assert outcome == "waiting_for_human"
        assert evidence[0].startswith("hook reported idle, but the terminal shows a dialog")
        assert any("Resume from summary" in line for line in evidence)


class TestResumeBySessionId:
    async def test_the_session_the_backbone_last_saw_is_reopened(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        project = tmp_path / "project"
        project.mkdir()
        write_state_file(
            config.state_dir,
            "ike",
            {"state": "unknown", "ts": 1.0, "session_id": "01a0-sess", "runtime": "claude"},
        )
        spec = AgentSpec(name="ike", dir=str(project), runtime="claude")
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
            patch(f"{_BASE}.resolve_command", return_value="/usr/bin/claude"),
        ):
            result = await start_agent(spec, config, resume=True, wait=False)
        command = start.await_args.kwargs["command"]
        assert command[command.index("--resume") + 1] == "01a0-sess"
        assert any("01a0-sess" in line for line in result.evidence)

    async def test_a_record_without_a_runtime_still_resumes(self, tmp_path):
        """An older state file, or a hook wired outside a backbone session: the
        id is this agent's own, so it is still used."""
        config = bootstrap_config(tmp_path / "data")
        project = tmp_path / "project"
        project.mkdir()
        write_state_file(
            config.state_dir, "ike", {"state": "unknown", "ts": 1.0, "session_id": "01a0-old"}
        )
        spec = AgentSpec(name="ike", dir=str(project), runtime="claude")
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
            patch(f"{_BASE}.resolve_command", return_value="/usr/bin/claude"),
        ):
            await start_agent(spec, config, resume=True, wait=False)
        command = start.await_args.kwargs["command"]
        assert command[command.index("--resume") + 1] == "01a0-old"

    @pytest.mark.parametrize("previous_runtime", ["claude", "opencode"])
    async def test_another_runtimes_session_id_is_not_handed_over(self, tmp_path, previous_runtime):
        """The agent was switched from Claude to Codex: Claude's id means nothing to Codex."""
        config = bootstrap_config(tmp_path / "data")
        project = tmp_path / "project"
        project.mkdir()
        write_state_file(
            config.state_dir,
            "ike",
            {"state": "unknown", "ts": 1.0, "session_id": "old-sess", "runtime": previous_runtime},
        )
        spec = AgentSpec(name="ike", dir=str(project), runtime="codex")
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
            patch(f"{_BASE}.resolve_command", return_value="/usr/bin/codex"),
        ):
            result = await start_agent(spec, config, resume=True, wait=False)
        assert start.await_args.kwargs["command"][1:3] == ["resume", "--last"]
        assert any(f"belongs to {previous_runtime}" in line for line in result.evidence)

    async def test_without_a_known_session_the_runtimes_own_resume_is_used(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        project = tmp_path / "project"
        project.mkdir()
        spec = AgentSpec(name="ike", dir=str(project), runtime="codex")
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
            patch(f"{_BASE}.resolve_command", return_value="/usr/bin/codex"),
        ):
            await start_agent(spec, config, resume=True, wait=False)
        assert start.await_args.kwargs["command"][1:3] == ["resume", "--last"]


class TestStartupHookAuthority:
    @pytest.mark.parametrize("state", ["busy", "blocked"])
    async def test_fresh_working_hook_beats_visible_input_prompt(self, tmp_path, state):
        write_state_file(tmp_path, "app", {"state": state, "ts": 100})
        with (
            patch(f"{_MOD}.session_exists", AsyncMock(return_value=True)),
            patch(f"{_MOD}.capture_pane", AsyncMock(return_value="❯")),
        ):
            outcome, _ = await wait_until_ready(
                "app", state_dir=tmp_path, runtime="claude", since=90, timeout=0
            )
        assert outcome == "timeout"

    async def test_busy_then_idle_hook_completes_start(self, tmp_path):
        from agent_backbone.services.agents.models import StateSnapshot

        with (
            patch(f"{_MOD}.session_exists", AsyncMock(return_value=True)),
            patch(f"{_MOD}.capture_pane", AsyncMock(return_value="❯")),
            patch(
                f"{_MOD}.read_state_file",
                side_effect=[
                    StateSnapshot(AgentState.BUSY, timestamp=100),
                    StateSnapshot(AgentState.IDLE, timestamp=101),
                ],
            ) as read,
        ):
            outcome, _ = await wait_until_ready(
                "app", state_dir=tmp_path, runtime="claude", since=90, poll_interval=0
            )
        assert outcome == "ready"
        assert read.call_count == 2


class TestHookWiringReachesTheSession:
    async def test_gemini_and_opencode_get_their_hook_environment(self, tmp_path):
        config = bootstrap_config(tmp_path / "data")
        project = tmp_path / "project"
        project.mkdir()
        for runtime, key in (
            ("gemini", "GEMINI_CLI_SYSTEM_SETTINGS_PATH"),
            ("opencode", "OPENCODE_CONFIG_CONTENT"),
        ):
            spec = AgentSpec(name=f"{runtime}-agent", dir=str(project), runtime=runtime)
            with (
                patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
                patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
                patch(f"{_BASE}.resolve_command", return_value=f"/usr/bin/{runtime}"),
            ):
                result = await start_agent(spec, config, wait=False)
            assert result.ok
            env = start.await_args.kwargs["environment"]
            assert key in env
            assert env["BACKBONE_AGENT"] == f"{runtime}-agent"


class TestLaunchEnvFromRuntime:
    async def test_runtime_environment_reaches_the_session(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        spec = AgentSpec(name="ike", dir=str(project), runtime="deepcode", model="deepseek-v4-pro")
        config = bootstrap_config(tmp_path / "data")
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
            patch(f"{_BASE}.resolve_command", return_value="/bin/deepcode"),
        ):
            assert (await start_agent(spec, config, wait=False)).ok
        env = start.await_args.kwargs["environment"]
        assert env["MODEL"] == "deepseek-v4-pro" and env["BACKBONE_RUNTIME"] == "deepcode"

    async def test_the_selected_model_beats_a_model_in_the_agents_env(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        spec = AgentSpec(
            name="ike", dir=str(project), runtime="deepcode", env={"MODEL": "deepseek-v4-flash"}
        )
        config = bootstrap_config(tmp_path / "data")
        with (
            patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=False),
            patch(f"{_MOD}.start_session", new_callable=AsyncMock, return_value=True) as start,
            patch(f"{_BASE}.resolve_command", return_value="/bin/deepcode"),
        ):
            await start_agent(spec, config, model="deepseek-v4-pro", wait=False)
        assert start.await_args.kwargs["environment"]["MODEL"] == "deepseek-v4-pro"
