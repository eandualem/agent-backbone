"""Webhook catch-up runs once; poll intake keeps its periodic job."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from agent_backbone.api.app import _register_jobs
from agent_backbone.config import AgentsConfig, AgentSpec, bootstrap_config


@pytest.mark.parametrize("intake,backfill", [("webhook", True), ("webhook", False), ("poll", True)])
async def test_only_enabled_webhook_backfill_is_a_one_shot(tmp_path, intake, backfill):
    config = bootstrap_config(tmp_path)
    config = replace(
        config,
        github_token="test-token",
        webhook_secret="test-secret" if intake == "webhook" else "",
        github=replace(config.github, intake=intake, backfill_on_start=backfill),
    )
    app = FastAPI()
    app.state.config = config
    app.state.github = AsyncMock()
    app.state.db = AsyncMock()
    app.state.integrations = SimpleNamespace(
        sync_agents=AsyncMock(),
        reconcile=AsyncMock(),
        flush_reports=AsyncMock(),
        flush_report_audio=AsyncMock(),
    )
    app.state.issue_closed_hooks = ()
    with (
        patch("agent_backbone.services.jobs.UpgradeWatch"),
        patch("agent_backbone.services.jobs.GitHubPoller") as poller,
    ):
        poller.return_value.backfill = AsyncMock()
        scheduler = _register_jobs(app)
    jobs = scheduler._jobs
    if intake == "poll":
        assert "github-backfill" not in jobs
        assert not jobs["github-poll"].once
        assert jobs["github-poll"].interval == config.github.poll_interval_seconds
    elif backfill:
        job = jobs["github-backfill"]
        assert job.once and job.run_immediately
        await job.fn()
        poller.return_value.backfill.assert_awaited_once()
        assert job.interval == 0
    else:
        assert "github-backfill" not in jobs and "github-poll" not in jobs


async def test_upgrade_requests_server_exit_without_sending_a_signal(tmp_path):
    from unittest.mock import MagicMock

    app = FastAPI()
    app.state.config = bootstrap_config(tmp_path)
    app.state.github = None
    app.state.db = AsyncMock()
    app.state.integrations = SimpleNamespace(
        reconcile=AsyncMock(), flush_reports=AsyncMock(), flush_report_audio=AsyncMock()
    )
    shutdown = MagicMock()
    app.state.request_shutdown = shutdown
    with patch("agent_backbone.services.jobs.UpgradeWatch") as watch:
        _register_jobs(app)
    with patch("os.kill") as kill:
        await watch.call_args.kwargs["restart"]()
    assert app.state.restart_requested is True
    shutdown.assert_called_once_with()
    kill.assert_not_called()
    del app.state.request_shutdown
    assert not watch.call_args.kwargs["enabled"]()


async def test_failed_startup_backfill_retries_only_unfinished_repos_on_existing_retry_job(
    tmp_path, db
):
    config = bootstrap_config(tmp_path)
    config = replace(
        config,
        github_token="test-token",
        webhook_secret="test-secret",
        github=replace(config.github, intake="webhook", reviewers=()),
        agents=AgentsConfig(
            {"a": AgentSpec(name="a", dir=str(tmp_path), repo="acme/bad", watches=("acme/good",))}
        ),
    )
    gh = AsyncMock()
    failed = False

    async def issues(repo, since):
        nonlocal failed
        if repo == "acme/bad" and not failed:
            failed = True
            raise TimeoutError("startup outage")
        return []

    gh.list_issues_since.side_effect = issues
    gh.list_comments_since.return_value = []
    app = FastAPI()
    app.state.config = config
    app.state.db = db
    app.state.github = gh
    app.state.issue_closed_hooks = ()
    app.state.integrations = SimpleNamespace(
        reconcile=AsyncMock(), flush_reports=AsyncMock(), flush_report_audio=AsyncMock()
    )
    with (
        patch("agent_backbone.services.jobs.UpgradeWatch"),
        patch(
            "agent_backbone.services.jobs.delivery_retry", return_value={"normal_retry": 1}
        ) as retry,
    ):
        scheduler = _register_jobs(app)
    backfill = scheduler._jobs["github-backfill"]
    await scheduler._run_once(backfill)
    assert backfill.status.failures == 1 and backfill.status.last_error
    assert await scheduler._jobs["delivery-retry"].fn() == {"normal_retry": 1}
    assert backfill.status.runs == 2 and backfill.status.last_error is None
    await scheduler._jobs["delivery-retry"].fn()
    assert retry.await_count == 2
    assert [call.args[0] for call in gh.list_issues_since.await_args_list] == [
        "acme/bad",
        "acme/good",
        "acme/bad",
    ]
    observations = [
        row for row in await db.diagnostics.query() if row["source"] == "github-backfill"
    ]
    assert {row["code"] for row in observations} == {"run_failed", "run_recovered"}


async def test_job_started_inside_another_run_does_not_inherit_its_config(tmp_path, db):
    import asyncio

    app = FastAPI()
    app.state.config = bootstrap_config(tmp_path)
    app.state.db = db
    app.state.github = None
    captured, release = asyncio.Event(), asyncio.Event()
    later = []
    observed = []

    async def resource(config, database):
        observed.append(config)

    async def nested():
        async def run_later():
            captured.set()
            await release.wait()
            await scheduler._jobs["resource-sample"].fn()

        later.append(asyncio.create_task(run_later()))
        await captured.wait()

    app.state.integrations = SimpleNamespace(
        reconcile=nested, flush_reports=AsyncMock(), flush_report_audio=AsyncMock()
    )
    with patch("agent_backbone.services.jobs.sample_resources", resource):
        scheduler = _register_jobs(app)
    await scheduler._jobs["integrations-sync"].fn()
    refreshed = replace(app.state.config, settings={"diagnostics.mode": "off"})
    app.state.config = refreshed
    release.set()
    await later[0]
    assert observed == [refreshed]


async def test_monitor_uses_its_fresh_roster_without_changing_provider(tmp_path, db):
    app = FastAPI()
    app.state.config = bootstrap_config(tmp_path)
    app.state.db = db
    app.state.github = None
    refreshed = replace(
        app.state.config, agents=AgentsConfig({"new": AgentSpec(name="new", dir=str(tmp_path))})
    )

    async def refresh():
        app.state.config = refreshed

    app.state.agent_store = SimpleNamespace(refresh=refresh)
    app.state.integrations = SimpleNamespace(
        reconcile=AsyncMock(), flush_reports=AsyncMock(), flush_report_audio=AsyncMock()
    )
    monitor = AsyncMock()
    with patch("agent_backbone.services.jobs.monitor_agents", monitor):
        scheduler = _register_jobs(app)
    await scheduler._jobs["agent-monitor"].fn()
    assert monitor.call_args.args[0] is refreshed


async def test_job_collaborators_keep_snapshot_then_return_to_live_config(tmp_path, db):
    import asyncio

    from agent_backbone.api.app import _job_configuration
    from agent_backbone.services.integrations.base import Integration
    from agent_backbone.services.sources.base import Source

    app = FastAPI()
    original = bootstrap_config(tmp_path)
    replacement = replace(original, settings={"diagnostics.mode": "off"})
    app.state.config, app.state.db, app.state.github = original, db, None
    integration = Integration(lambda: _job_configuration(app.state))
    source = Source(lambda: _job_configuration(app.state))
    seen = []

    async def flush():
        seen.append((integration.config, source.config))
        app.state.config = replacement
        await asyncio.sleep(0)
        seen.append((integration.config, source.config))

    app.state.integrations = SimpleNamespace(
        reconcile=AsyncMock(), flush_reports=flush, flush_report_audio=AsyncMock()
    )
    scheduler = _register_jobs(app)
    await scheduler._jobs["report-notifications"].fn()
    assert seen == [(original, original), (original, original)]
    assert integration.config is replacement
    assert source.config is replacement
    seen.clear()
    await scheduler._jobs["report-notifications"].fn()
    assert seen == [(replacement, replacement), (replacement, replacement)]
