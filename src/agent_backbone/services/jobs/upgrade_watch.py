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

from agent_backbone.release import Installation, code_identity, installation, same_line

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
        self._holds: set[str] = set()

    def set_hold(self, operation: str, enabled: bool) -> dict:
        """Suppress automatic restarts until this process ends or the operation releases."""
        if enabled:
            if self.requested:
                raise ValueError("automatic restart already requested; retry after restart")
            if operation not in self._holds and len(self._holds) >= 32:
                raise ValueError("too many upgrade holds; restart the service to clear them")
            self._holds.add(operation)
        else:
            self._holds.discard(operation)
        return {"held": bool(self._holds), "operation_held": operation in self._holds}

    async def run(self) -> dict:
        if self.requested:
            return {"restart": "requested"}
        current = await asyncio.to_thread(self._identity, self._install)
        if current == self.started:
            return {"code": current}
        if not same_line(self.started, current):
            # The checkout is on another branch: someone is developing in it.
            return {"code": current, "changed_from": self.started, "restart": "other branch"}
        if self._holds:
            return {"code": current, "changed_from": self.started, "restart": "held"}
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
