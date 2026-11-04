"""Persistent integration tasks see revocations and coalesced config publishes."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.api.app import create_app, lifespan
from agent_backbone.services.integrations import Integration, Integrations


@pytest.mark.parametrize("trigger", ["job", "publish"])
async def test_integration_polling_keeps_live_config_after_guarded_start(config, trigger):
    initial = replace(
        config, github_token="", telegram=replace(config.telegram, allowed_user_ids=(1,))
    )
    revoked = replace(initial, telegram=replace(initial.telegram, allowed_user_ids=()))
    app = create_app(initial).other_asgi_app
    sync_entered, sync_release, poll_read = (asyncio.Event() for _ in range(3))
    synced, polled = [], []
    poller = None

    class Probe(Integration):
        name = "probe"

        @property
        def enabled(self):
            return True

        async def start(self):
            nonlocal poller
            self._running = True

            async def polling():
                await poll_read.wait()
                polled.append(self.config.telegram.allowed_user_ids)

            # Like an integration's persistent update/polling task, this
            # inherits the context of the callback that starts the service.
            poller = asyncio.create_task(polling())

        async def sync_agents(self):
            synced.append(self.config.telegram.allowed_user_ids)
            if trigger == "publish" and len(synced) == 1:
                sync_entered.set()
                await sync_release.wait()

        async def stop(self):
            self._running = False
            poll_read.set()
            if poller is not None:
                await poller

    def integrations(supplier, db, *, operation):
        return Integrations([Probe(supplier, db, operation=operation)], operation=operation)

    with (
        patch("agent_backbone.api.app.LifecycleManager") as lifecycle,
        patch("agent_backbone.services.database.check_profile_startup", new=AsyncMock()),
        patch("agent_backbone.services.database.BackboneDB"),
        patch("agent_backbone.services.agents.AgentStore") as store,
        patch("agent_backbone.hooks.install.install_hook_files"),
        patch("agent_backbone.api.socketio_server.configure_pty_manager"),
        patch("agent_backbone.api.socketio_server.get_pty_manager"),
        patch("agent_backbone.api.session_updates.SessionFeed"),
        patch("agent_backbone.services.integrations.build_integrations", integrations),
        patch(
            "agent_backbone.services.sources.build_sources",
            return_value=SimpleNamespace(enabled=[]),
        ),
    ):
        lifecycle.return_value.start_all = AsyncMock()
        lifecycle.return_value.stop_all = AsyncMock()
        async with lifespan(app):
            publish = store.call_args.kwargs["on_change"]
            try:
                if trigger == "job":
                    await app.state.scheduler._jobs["integrations-sync"].fn()
                    publish(revoked)
                else:
                    publish(initial)
                    await asyncio.wait_for(sync_entered.wait(), 2)
                    publish(revoked)  # coalesces while the first sync waits
                sync_release.set()
                await asyncio.gather(*app.state.integrations._background)
                poll_read.set()
                await poller
                assert synced == [(1,), ()]
                assert polled == [()]
            finally:
                sync_release.set()
                await app.state.integrations.stop()
