"""Webhook catch-up runs once; poll intake keeps its periodic job."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from agent_backbone.api.app import _register_jobs
from agent_backbone.config import bootstrap_config


@pytest.mark.parametrize("intake,backfill", [("webhook", True), ("webhook", False), ("poll", True)])
def test_only_enabled_webhook_backfill_is_a_one_shot(tmp_path, intake, backfill):
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
        scheduler = _register_jobs(app)
    jobs = scheduler._jobs
    if intake == "poll":
        assert "github-backfill" not in jobs
        assert not jobs["github-poll"].once
        assert jobs["github-poll"].interval == config.github.poll_interval_seconds
    elif backfill:
        job = jobs["github-backfill"]
        assert job.once and job.run_immediately
        assert job.fn is poller.return_value.run
        assert job.interval == 0
    else:
        assert "github-backfill" not in jobs and "github-poll" not in jobs


async def test_upgrade_requests_server_exit_without_sending_a_signal(tmp_path):
    from unittest.mock import MagicMock

    app = FastAPI()
    app.state.config = bootstrap_config(tmp_path)
    app.state.github = None
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
