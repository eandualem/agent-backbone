"""Restart onto new code when the code on disk changes.

The backbone is a plain process; agents are tmux sessions and the queue is
in the database, so a restart loses nothing but a few seconds of API.
What was missing was someone to perform it. This job compares what a
fresh process would run (``release.code_identity``: the checkout's commit
for a development install, the installed version otherwise) with what
this process started as, and asks the API for a restart when they differ
and nothing is being routed. ``backbone.restart_on_upgrade`` turns it off.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from agent_backbone.release import Installation, code_identity, installation

log = logging.getLogger(__name__)


class UpgradeWatch:
    def __init__(
        self,
        *,
        enabled: Callable[[], bool],
        restart: Callable[[], Awaitable[None]],
        in_flight: Callable[[], int],
        identity: Callable[[Installation], str] = code_identity,
        install: Installation | None = None,
    ) -> None:
        self._enabled = enabled
        self._restart = restart
        self._in_flight = in_flight
        self._identity = identity
        self._install = install or installation()
        self.started = identity(self._install)
        self.requested = False

    async def run(self) -> dict:
        if self.requested:
            return {"restart": "requested"}
        current = await asyncio.to_thread(self._identity, self._install)
        if current == self.started:
            return {"code": current}
        if not self._enabled():
            return {"code": current, "changed_from": self.started, "restart": "disabled"}
        pending = self._in_flight()
        if pending:
            return {
                "code": current,
                "changed_from": self.started,
                "restart": f"deferred ({pending} in flight)",
            }
        log.info("Code on disk changed (%s -> %s): restarting onto it", self.started, current)
        self.requested = True
        await self._restart()
        return {"code": current, "changed_from": self.started, "restart": "requested"}
