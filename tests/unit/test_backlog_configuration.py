"""Behavioral regressions for the consolidated configuration backlog."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from agent_backbone.api.app import _register_jobs
from agent_backbone.config import AgentsConfig, AgentSpec, build_config, validate_setting
from agent_backbone.help import render_agent_brief
from agent_backbone.services.agents import AgentStore, approve_agent
from agent_backbone.services.integrations import Integration, Integrations
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.runtimes.claude import pre_accept_bypass
from agent_backbone.services.scheduler import PeriodicScheduler


@pytest.mark.parametrize("question", ["Choose a session", "Quick safety check", "Unknown picker"])
async def test_unnumbered_choices_block_delivery_and_approval(question):
    pane = f"{question}\n ❯ No, exit\n   Yes, continue\n Enter to confirm · Esc to cancel"
    rt = RUNTIMES["claude"]
    assert rt.detect_active_dialog(pane)
    assert not rt.detect_idle(pane)
    with (
        patch("agent_backbone.services.agents.launch.session_exists", return_value=True),
        patch("agent_backbone.services.agents.launch.capture_pane", return_value=pane),
        patch("agent_backbone.services.runtimes.base.send_keys") as keys,
    ):
        outcome, _ = await approve_agent("app", runtime="claude")
    assert outcome == "not_permission"
    keys.assert_not_called()
    assert not rt.detect_active_dialog(pane + "\n❯ ")


def test_bypass_consent_preserves_config_and_handles_invalid_json(tmp_path):
    path = tmp_path / "claude.json"
    path.write_text('{"projects":{"a":{"hasTrustDialogAccepted":true}},"other":42}')
    assert pre_accept_bypass(claude_config=path)
    saved = json.loads(path.read_text())
    assert saved["bypassPermissionsModeAccepted"] is True
    assert saved["other"] == 42 and saved["projects"]["a"]["hasTrustDialogAccepted"]
    path.write_text("broken")
    assert not pre_accept_bypass(claude_config=path)
    assert path.read_text() == "broken"


def test_policy_order_literal_content_and_full_override(tmp_path):
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "pr.md").write_text("Review {literal} before merging.")
    (policies / "work.md").write_text("Record the result.")
    brief = render_agent_brief({"agent_name": "a"}, tmp_path, policy_names=("pr", "work"))
    assert "backbone help" in brief
    assert brief.index("{literal}") < brief.index("Record the result")
    with pytest.raises(ValueError, match="Cannot read configured"):
        render_agent_brief({}, tmp_path, policy_names=("missing",))
    with pytest.raises(ValueError):
        validate_setting("agents.shared_policy", ["../secrets"])
    (tmp_path / "agent-brief.md").write_text("Full control")
    assert render_agent_brief({}, tmp_path, policy_names=("missing",)) == "Full control"


@pytest.mark.parametrize(
    "change",
    [
        {"runtime": "typo"},
        {"model": ":high"},
        {"model": "opus:impossible"},
        {"repo": "bad/repo/path"},
        {"tags": "one"},
        {"env": {"OK": 7}},
        {"dir": ""},
    ],
)
async def test_invalid_agent_updates_are_atomic(db, tmp_path, change):
    store = AgentStore(db, tmp_path)
    await store.start()
    original = await store.register(AgentSpec(name="a", dir=str(tmp_path)))
    with pytest.raises(ValueError):
        await store.update("a", description="must not persist", **change)
    await store.refresh()
    assert store.agents.get("a") == original


async def test_invalid_legacy_agent_is_visible_and_can_be_repaired(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="a", dir=str(tmp_path)))
    await db.agents.update_fields("a", {"runtime": "old-runtime"})
    await store.refresh()
    assert store.agents.get("a").runtime == "old-runtime"
    assert (await store.update("a", runtime="shell")).runtime == "shell"


async def test_schedule_changes_preserve_active_run_and_wake_sleepers():
    scheduler = PeriodicScheduler()
    entered, release, again = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def job():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        else:
            again.set()

    scheduler.add("work", 3600, job, run_immediately=True)
    await scheduler.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        scheduler.configure("work", 1, job, enabled=False)
        assert scheduler.jobs == []
        assert not scheduler._jobs["work"].task.done()
        release.set()
        await scheduler._jobs["work"].task
        scheduler.configure("work", 3600, job, run_immediately=False)
        await asyncio.sleep(0)
        scheduler.configure("work", 0.001, job)
        await asyncio.wait_for(again.wait(), 1)
        assert calls >= 2
    finally:
        await scheduler.stop()


async def test_off_to_poll_and_intervals_reconcile(tmp_path):
    from types import SimpleNamespace

    app = FastAPI()
    app.state.config = build_config(
        tmp_path, settings={"github.intake": "off"}, agents=AgentsConfig()
    )
    app.state.github = AsyncMock()
    app.state.db = AsyncMock()
    app.state.integrations = SimpleNamespace(reconcile=AsyncMock())
    app.state.issue_closed_hooks = ()
    scheduler = _register_jobs(app)
    assert "github-poll" not in {j.name for j in scheduler.jobs}
    app.state.config = replace(
        app.state.config,
        github_token="test",
        github=replace(app.state.config.github, intake="poll", poll_interval_seconds=17),
    )
    app.state.reconcile_jobs()
    assert next(j for j in scheduler.jobs if j.name == "github-poll").interval_seconds == 17
    app.state.config = replace(
        app.state.config, github=replace(app.state.config.github, intake="off")
    )
    app.state.reconcile_jobs()
    assert "github-poll" not in {j.name for j in scheduler.jobs}


async def test_integration_enable_disable_is_serialized(config):
    class Channel(Integration):
        allowed = False
        starts = 0
        stops = 0

        @property
        def enabled(self):
            return self.allowed

        async def start(self):
            await asyncio.sleep(0)
            self.starts += 1
            self._running = True

        async def stop(self):
            self.stops += 1
            self._running = False

    channel = Channel(config)
    integrations = Integrations([channel])
    await integrations.reconcile()
    assert channel.starts == 0
    channel.allowed = True
    await asyncio.gather(integrations.reconcile(), integrations.reconcile())
    assert channel.starts == 1
    channel.allowed = False
    await integrations.reconcile()
    assert channel.stops == 1


async def test_numbered_claude_dialog_with_modern_footer_can_be_approved():
    pane = "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Enter to confirm · Esc to cancel"
    rt = RUNTIMES["claude"]
    assert rt.detect_active_dialog(pane) and not rt.detect_choice_dialog(pane)
    with (
        patch("agent_backbone.services.agents.launch.session_exists", return_value=True),
        patch("agent_backbone.services.agents.launch.capture_pane", side_effect=[pane, "❯ "]),
        patch("agent_backbone.services.runtimes.base.send_keys", return_value=True) as keys,
    ):
        outcome, _ = await approve_agent("app", runtime="claude", settle_seconds=0)
    assert outcome == "approved"
    keys.assert_awaited_once()
