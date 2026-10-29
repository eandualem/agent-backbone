"""The periodic prune job includes queue retention and reads live settings."""

import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI

from agent_backbone.api.app import _register_jobs
from agent_backbone.config import bootstrap_config


async def test_prune_job_uses_live_delivery_retention_for_queue(tmp_path):
    event_thread = threading.get_ident()

    def rotate(*args, **kwargs):
        assert threading.get_ident() != event_thread
        assert kwargs == {"receipt_retention_days": 9}
        return 5

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
        state_history=SimpleNamespace(prune=AsyncMock(return_value=10)),
        queue=SimpleNamespace(
            prune=AsyncMock(return_value=4), active_ids=AsyncMock(return_value=set())
        ),
        diagnostics=SimpleNamespace(prune=AsyncMock(return_value=6)),
        current_plans=SimpleNamespace(prune=AsyncMock(return_value=11)),
        signing=SimpleNamespace(
            prune=AsyncMock(return_value=8), prune_receipts=AsyncMock(return_value=9)
        ),
    )
    manager = MagicMock()
    manager.attach_mock(app.state.db.signing.prune_receipts, "receipts")
    manager.attach_mock(app.state.db.deliveries.prune, "deliveries")
    manager.attach_mock(app.state.db.queue.active_ids, "active_ids")
    manager.attach_mock(app.state.db.queue.prune, "queue")
    with (
        patch("agent_backbone.services.scheduler.PeriodicScheduler") as scheduler,
        patch("agent_backbone.services.jobs.UpgradeWatch"),
        patch("agent_backbone.services.agents.rotate_action_log", side_effect=rotate),
        patch("agent_backbone.api.app.batch_receipts", return_value=[tmp_path / "1.taken"]),
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
            "state_history": 10,
            "queue": 4,
            "context_receipts": 1,
            "diagnostics": 6,
            "current_plans": 11,
            "signing_audit": 8,
            "signing_receipts": 9,
            "action_log_lines": 5,
        }
    for repo in (
        app.state.db.deliveries,
        app.state.db.events,
        app.state.db.queue,
        app.state.db.diagnostics,
        app.state.db.current_plans,
        app.state.db.signing,
    ):
        repo.prune.assert_awaited_once_with(9)
    app.state.db.state_history.prune.assert_awaited_once_with()
    # Receipts take their delivery times from records that the deliveries prune removes.
    order = [c[0] for c in manager.mock_calls if c[0] in ("receipts", "deliveries")]
    assert order == ["receipts", "deliveries"]
    # Hook receipts go before the queue rows whose ids pruning frees.
    order = [c[0] for c in manager.mock_calls if c[0] in ("active_ids", "queue")]
    assert order == ["active_ids", "queue"]
