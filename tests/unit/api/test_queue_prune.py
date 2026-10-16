"""The periodic prune job includes queue retention and reads live settings."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI

from agent_backbone.api.app import _register_jobs
from agent_backbone.config import bootstrap_config


async def test_prune_job_uses_live_delivery_retention_for_queue(tmp_path):
    app = FastAPI()
    app.state.config = bootstrap_config(tmp_path)
    app.state.github = None
    app.state.integrations = SimpleNamespace(
        sync_agents=AsyncMock(),
        reconcile=AsyncMock(),
        flush_reports=AsyncMock(),
        flush_report_audio=AsyncMock(),
    )
    app.state.db = SimpleNamespace(
        deliveries=SimpleNamespace(prune=AsyncMock(return_value=2)),
        events=SimpleNamespace(prune=AsyncMock(return_value=3)),
        queue=SimpleNamespace(prune=AsyncMock(return_value=4)),
        diagnostics=SimpleNamespace(prune=AsyncMock(return_value=6)),
        reports=SimpleNamespace(prune=AsyncMock(return_value=7)),
        signing=SimpleNamespace(
            prune=AsyncMock(return_value=8), prune_receipts=AsyncMock(return_value=9)
        ),
    )
    manager = MagicMock()
    manager.attach_mock(app.state.db.signing.prune_receipts, "receipts")
    manager.attach_mock(app.state.db.deliveries.prune, "deliveries")
    with (
        patch("agent_backbone.services.scheduler.PeriodicScheduler") as scheduler,
        patch("agent_backbone.services.jobs.UpgradeWatch"),
        patch("agent_backbone.services.agents.rotate_action_log", return_value=5),
    ):
        _register_jobs(app)
        job = next(
            call for call in scheduler.return_value.add.call_args_list if call.args[0] == "prune"
        )
        assert job.args[1] == 6 * 3600
        config = app.state.config
        app.state.config = replace(config, timing=replace(config.timing, delivery_retention_days=9))
        assert await job.args[2]() == {
            "deliveries": 2,
            "events": 3,
            "queue": 4,
            "diagnostics": 6,
            "reports": 7,
            "signing_audit": 8,
            "signing_receipts": 9,
            "action_log_lines": 5,
        }
    for repo in (
        app.state.db.deliveries,
        app.state.db.events,
        app.state.db.queue,
        app.state.db.diagnostics,
        app.state.db.reports,
        app.state.db.signing,
    ):
        repo.prune.assert_awaited_once_with(9)
    # Receipts take their delivery times from records that the deliveries prune removes.
    order = [c[0] for c in manager.mock_calls if c[0] in ("receipts", "deliveries")]
    assert order == ["receipts", "deliveries"]
