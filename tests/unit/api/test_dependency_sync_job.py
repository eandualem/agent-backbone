"""Dependency sync is its own job, so a monitor pass never waits on its listings."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI

from agent_backbone.api.app import _register_jobs
from agent_backbone.config import bootstrap_config
from agent_backbone.services.github import QueueSnapshot


async def test_dependency_sync_runs_every_ten_minutes_on_its_own_snapshot(tmp_path):
    app = FastAPI()
    app.state.config = bootstrap_config(tmp_path)
    app.state.github = AsyncMock()
    app.state.db = AsyncMock()
    app.state.issue_closed_hooks = ()
    app.state.integrations = SimpleNamespace(
        reconcile=AsyncMock(), flush_reports=AsyncMock(), flush_report_audio=AsyncMock()
    )
    with (
        patch("agent_backbone.services.jobs.UpgradeWatch"),
        patch("agent_backbone.services.jobs.GitHubPoller"),
        patch("agent_backbone.services.routing.sync_dependencies", new_callable=AsyncMock) as sync,
    ):
        scheduler = _register_jobs(app)
        job = scheduler._jobs["dependency-sync"]
        assert job.interval == 600 and job.run_immediately
        await job.fn()
    config, db, gh = sync.await_args.args
    assert (config, db) == (app.state.config, app.state.db)
    assert isinstance(gh, QueueSnapshot) and gh.client is app.state.github
