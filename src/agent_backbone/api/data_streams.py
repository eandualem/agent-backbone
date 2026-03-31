"""Real-time data stream namespaces — Socket.IO push for all dashboard domains.

Implements the REALTIME_DATA_STREAMS protocol (RDS-1 through RDS-82).
Each namespace follows a uniform pattern: authenticate, snapshot, change-detect, emit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import socketio

log = logging.getLogger(__name__)


def _socket_auth_valid(auth: dict | None = None) -> bool:
    """Validate Socket.IO auth against the configured API key (RDS-2)."""
    import hmac
    import os

    api_key = os.environ.get("BACKBONE_API_KEY", "")
    if not api_key:
        return True
    raw = auth.get("api_key") if isinstance(auth, dict) else None
    token = raw if isinstance(raw, str) else ""
    return hmac.compare_digest(token, api_key)


def _signature(data: Any) -> str:
    """Compute a stable signature for change detection (RDS-5)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class DataStreamNamespace(socketio.AsyncNamespace):
    """Reusable Socket.IO namespace for a data domain.

    Handles auth, change detection, and emission. Each instance is
    configured with a fetch function that returns the snapshot payload.
    """

    def __init__(
        self,
        namespace: str,
        event_name: str,
        fetch_fn: Callable[[], Awaitable[Any]],
    ) -> None:
        super().__init__(namespace)
        self.event_name = event_name
        self.fetch_fn = fetch_fn
        self._last_signature: str | None = None
        self._lock = asyncio.Lock()

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        """Authenticate connection (RDS-2)."""
        if not _socket_auth_valid(auth):
            log.warning("Socket.IO %s rejected (sid=%s)", self.namespace, sid)
            return False
        asyncio.create_task(
            self.emit_update(force=True, to=sid),
            name=f"ds-bootstrap-{self.namespace}-{sid}",
        )
        return True

    async def emit_update(self, *, force: bool = False, to: str | None = None) -> bool:
        """Fetch snapshot and emit if changed (RDS-3, RDS-4, RDS-5).

        Args:
            force: If True, emit regardless of change detection (imperative trigger).
            to: If provided, emit only to the targeted Socket.IO sid.
        """
        async with self._lock:
            try:
                payload = await self.fetch_fn()
            except Exception:
                log.warning("Fetch failed for %s", self.namespace, exc_info=True)
                return False

            sig = _signature(payload)
            if not force and sig == self._last_signature:
                return False

            self._last_signature = sig
            kwargs: dict[str, Any] = {"namespace": self.namespace}
            if to is not None:
                kwargs["to"] = to
            await self.server.emit(self.event_name, payload, **kwargs)
            return True

    def reset(self) -> None:
        """Reset change detection state (for testing)."""
        self._last_signature = None


class RunsNamespace(socketio.AsyncNamespace):
    """Discrete run lifecycle event stream on the /runs namespace."""

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        """Authenticate connection using the shared Socket.IO API key."""
        if not _socket_auth_valid(auth):
            log.warning("Socket.IO %s rejected (sid=%s)", self.namespace, sid)
            return False
        return True

    async def on_subscribe(self, sid: str, data: dict) -> None:
        """Join per-run rooms for targeted run-event delivery."""
        if not isinstance(data, dict):
            return
        for run_id in data.get("runs", []):
            if isinstance(run_id, str) and run_id:
                await self.enter_room(sid, f"run:{run_id}")
        rooms = self.rooms(sid) or []
        filtered = [room for room in rooms if room != sid]
        await self.emit("subscribed", {"rooms": filtered}, to=sid)

    async def on_unsubscribe(self, sid: str, data: dict) -> None:
        """Leave per-run rooms."""
        if not isinstance(data, dict):
            return
        for run_id in data.get("runs", []):
            if isinstance(run_id, str) and run_id:
                await self.leave_room(sid, f"run:{run_id}")
        rooms = self.rooms(sid) or []
        filtered = [room for room in rooms if room != sid]
        await self.emit("subscribed", {"rooms": filtered}, to=sid)


async def _periodic_issue_sync(sio: socketio.AsyncServer, interval: float) -> None:
    """Periodically refresh issue projections from GitHub."""
    while True:
        try:
            await asyncio.sleep(interval)
            app = getattr(sio, "fastapi_app", None)
            issue_service = getattr(getattr(app, "state", None), "issue_service", None)
            if issue_service is not None:
                await issue_service.sync_inventory(emit_changes=True)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Periodic issue sync failed")


# --- Registry of all data stream namespaces ---

_namespaces: dict[str, DataStreamNamespace] = {}
_poll_tasks: list[asyncio.Task] = []


def get_data_namespace(name: str) -> DataStreamNamespace | None:
    """Look up a registered data stream namespace by domain name."""
    return _namespaces.get(name)


async def notify_stream(domain: str, *, force: bool = True) -> None:
    """Trigger an imperative update on a data stream namespace.

    Called from route handlers after mutations (RDS imperative triggers).
    """
    ns = _namespaces.get(domain)
    if ns is not None:
        await ns.emit_update(force=force)


async def _periodic_emitter(ns: DataStreamNamespace, interval: float) -> None:
    """Background loop that periodically emits updates with change detection."""
    while True:
        try:
            await asyncio.sleep(interval)
            await ns.emit_update()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Periodic emit failed for %s", ns.namespace)


def register_data_streams(sio: socketio.AsyncServer) -> None:
    """Register all data stream namespaces and start periodic emitters.

    Called during app lifespan after services are initialized.
    """

    # --- Fetch functions for each namespace ---

    async def fetch_agents() -> dict:
        """RDS-10/11: DashboardResponse payload."""
        from agent_backbone.api.routes.dashboard import (
            _compute_counts,
            _fetch_agents,
            _fetch_failed_deliveries,
            _fetch_issues_pending,
            _fetch_service_health,
        )
        from agent_backbone.services._locator import get_config, get_db
        from agent_backbone.services.agents.interface import StateService
        from agent_backbone.services.terminal import TmuxService

        config = get_config()
        db = get_db()
        state_svc = StateService(db=db)
        issue_service = sio.fastapi_app.state.issue_service
        tmux_svc = TmuxService()
        agents = await _fetch_agents(config, state_svc, tmux_svc)
        counts = _compute_counts(agents)
        failed, issues_pending, services = await asyncio.gather(
            _fetch_failed_deliveries(db),
            _fetch_issues_pending(issue_service),
            _fetch_service_health(db),
        )
        return {
            "agents": [a.model_dump(mode="json") for a in agents],
            "counts": counts.model_dump(mode="json"),
            "plans_pending": counts.plan_waiting,
            "issues_pending": issues_pending,
            "failed_deliveries": failed,
            "services": services.model_dump(mode="json"),
        }

    async def fetch_rooms() -> dict:
        """RDS-30/31: ListEnvelope[Room]."""
        from agent_backbone.api.routes.rooms import _list_rooms

        rooms = await _list_rooms()
        return {
            "items": [r.model_dump(mode="json") for r in rooms],
            "total": len(rooms),
        }

    async def fetch_activity() -> dict:
        """RDS-40/41: ListEnvelope[ActivityEvent]."""
        from agent_backbone.api.routes.activity import (
            _load_delivery_events,
            _load_heartbeat_events,
            _load_telemetry_events,
        )
        from agent_backbone.services._locator import get_db

        db = get_db()
        limit = 50
        all_events = (
            await _load_delivery_events(db, limit)
            + await _load_heartbeat_events(db, limit)
            + await _load_telemetry_events(db, limit)
        )
        all_events.sort(key=lambda e: e.ts, reverse=True)
        items = [e.model_dump(mode="json") for e in all_events[:limit]]
        return {"items": items, "total": len(items)}

    async def fetch_swarms() -> dict:
        """RDS-55/56: ListEnvelope[SwarmSummaryResponse]."""
        from agent_backbone.services._locator import get_db

        db = get_db()
        swarms = await db.list_swarms()
        visible = [s for s in swarms if s.get("phase") != "discarded"]
        return {"items": visible, "total": len(visible)}

    async def fetch_repos() -> dict:
        """RDS-65/66: ListEnvelope[RepoStatusResponse]."""
        from agent_backbone.api.routes.repos import _status_to_response
        from agent_backbone.services.automation.interface import OnboardingService

        svc = OnboardingService()
        repos = svc.discover_repos()
        items = []
        for entry in repos:
            status = svc.run_status_checks(entry.org, entry.repo)
            items.append(_status_to_response(status).model_dump(mode="json"))
        return {"items": items, "total": len(items)}

    async def fetch_settings() -> dict:
        """RDS-80/81: ServiceHealth."""
        from agent_backbone.api.routes.dashboard import _fetch_service_health
        from agent_backbone.services._locator import get_db

        db = get_db()
        health = await _fetch_service_health(db)
        return {"services": health.model_dump(mode="json")}

    # --- Register namespaces (RDS-1) ---
    specs: list[tuple[str, str, str, Callable[[], Awaitable[Any]], float]] = [
        ("agents", "/agents", "agents:update", fetch_agents, 30),
        ("rooms", "/rooms", "rooms:update", fetch_rooms, 10),
        ("activity", "/activity", "activity:update", fetch_activity, 15),
        ("swarms", "/swarms", "swarms:update", fetch_swarms, 30),
        ("repos", "/repos", "repos:update", fetch_repos, 60),
        ("settings", "/settings", "settings:update", fetch_settings, 30),
    ]

    for domain, ns_path, event, fn, interval in specs:
        ns = DataStreamNamespace(ns_path, event, fn)
        sio.register_namespace(ns)
        _namespaces[domain] = ns
        task = asyncio.create_task(
            _periodic_emitter(ns, interval),
            name=f"ds-{domain}",
        )
        _poll_tasks.append(task)

    from agent_backbone.api.issue_updates import ISSUES_NAMESPACE, IssuesNamespace
    from agent_backbone.services._locator import get_config

    sio.register_namespace(IssuesNamespace(ISSUES_NAMESPACE))
    sio.register_namespace(RunsNamespace("/runs"))
    issue_sync_task = asyncio.create_task(
        _periodic_issue_sync(sio, get_config().issue_domain.sync_interval_seconds),
        name="issue-sync",
    )
    _poll_tasks.append(issue_sync_task)
    log.info("Registered %d snapshot stream namespaces, /issues, and /runs", len(specs))


async def stop_data_streams() -> None:
    """Cancel all periodic emitter tasks. Called during shutdown."""
    for task in _poll_tasks:
        task.cancel()
    for task in _poll_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _poll_tasks.clear()
    _namespaces.clear()
    log.info("Data stream tasks stopped")
