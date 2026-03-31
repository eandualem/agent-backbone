"""Socket.IO issue updates and room routing for the canonical issue domain."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os

import socketio

ISSUES_NAMESPACE = "/issues"
ISSUES_UPDATE_EVENT = "issues:update"
ISSUE_DETAIL_UPDATE_EVENT = "issue:detail:update"

log = logging.getLogger(__name__)


def all_issues_room() -> str:
    return "all-issues"


def repo_room(repo_full_name: str) -> str:
    return f"repo:{repo_full_name}"


def issue_room(repo_full_name: str, issue_number: int) -> str:
    return f"issue:{repo_full_name}:{issue_number}"


def parse_issue_identifier(value: str) -> tuple[str, int] | None:
    repo_full_name, sep, issue_number_raw = value.rpartition(":")
    if not sep or not repo_full_name:
        return None
    try:
        issue_number = int(issue_number_raw)
    except ValueError:
        return None
    if issue_number <= 0:
        return None
    return repo_full_name, issue_number


def _socket_auth_valid(auth: dict | None = None) -> bool:
    api_key = os.environ.get("BACKBONE_API_KEY", "")
    if not api_key:
        return True
    raw = auth.get("api_key") if isinstance(auth, dict) else None
    token = raw if isinstance(raw, str) else ""
    return hmac.compare_digest(token, api_key)


class IssuesNamespace(socketio.AsyncNamespace):
    """Room-based issue subscription namespace."""

    def __init__(self, namespace: str | None = None) -> None:
        super().__init__(namespace)
        self._explicit_subscribers: set[str] = set()

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        if not _socket_auth_valid(auth):
            log.warning("Socket.IO issues connection rejected — invalid auth (sid=%s)", sid)
            return False
        await self.enter_room(sid, all_issues_room())
        asyncio.create_task(
            self._bootstrap_snapshot(sid),
            name=f"issues-bootstrap-{sid}",
        )
        return True

    async def _bootstrap_snapshot(self, sid: str) -> None:
        """Send the full open-issue list to a newly connected client."""
        try:
            app = getattr(self.server, "fastapi_app", None)
            issue_service = getattr(
                getattr(app, "state", None), "issue_service", None
            )
            if issue_service is None:
                return
            items, total = await issue_service.list_issues(
                state="open",
                repo_full_name=None,
                for_entity=None,
                from_entity=None,
                issue_type=None,
                label=None,
                page=1,
                per_page=500,
            )
            await self.emit(
                ISSUES_UPDATE_EVENT,
                items,
                to=sid,
            )
        except Exception:
            log.warning(
                "Issues bootstrap snapshot failed for sid=%s",
                sid,
                exc_info=True,
            )

    async def on_subscribe(self, sid: str, data: dict) -> None:
        if not isinstance(data, dict):
            return
        has_explicit_subscription = False

        for repo_full_name in data.get("repos", []):
            if isinstance(repo_full_name, str) and repo_full_name:
                await self.enter_room(sid, repo_room(repo_full_name))
                has_explicit_subscription = True

        for issue_identifier in data.get("issues", []):
            if not isinstance(issue_identifier, str):
                continue
            parsed = parse_issue_identifier(issue_identifier)
            if parsed is None:
                continue
            repo_full_name, issue_number = parsed
            await self.enter_room(sid, issue_room(repo_full_name, issue_number))
            has_explicit_subscription = True

        if data.get("all_issues"):
            await self.enter_room(sid, all_issues_room())
        elif has_explicit_subscription and sid not in self._explicit_subscribers:
            await self.leave_room(sid, all_issues_room())

        if has_explicit_subscription or data.get("all_issues"):
            self._explicit_subscribers.add(sid)

        rooms = self.rooms(sid) or []
        filtered = [room for room in rooms if room != sid]
        await self.emit("subscribed", {"rooms": filtered}, to=sid)

    async def on_unsubscribe(self, sid: str, data: dict) -> None:
        if not isinstance(data, dict):
            return

        for repo_full_name in data.get("repos", []):
            if isinstance(repo_full_name, str) and repo_full_name:
                await self.leave_room(sid, repo_room(repo_full_name))

        for issue_identifier in data.get("issues", []):
            if not isinstance(issue_identifier, str):
                continue
            parsed = parse_issue_identifier(issue_identifier)
            if parsed is None:
                continue
            repo_full_name, issue_number = parsed
            await self.leave_room(sid, issue_room(repo_full_name, issue_number))

        if data.get("all_issues"):
            await self.leave_room(sid, all_issues_room())

        rooms = self.rooms(sid) or []
        filtered = [room for room in rooms if room != sid]
        await self.emit("subscribed", {"rooms": filtered}, to=sid)

    async def on_disconnect(self, sid: str) -> None:
        self._explicit_subscribers.discard(sid)


async def emit_issue_change(
    sio: socketio.AsyncServer | None,
    *,
    summary: dict,
    detail: dict,
) -> None:
    """Emit one changed issue to the canonical /issues rooms."""
    if sio is None:
        return

    repo_full_name = summary.get("repo_full_name")
    issue_number = summary.get("number")
    if not isinstance(repo_full_name, str) or not isinstance(issue_number, int):
        return

    await sio.emit(
        ISSUES_UPDATE_EVENT,
        [summary],
        namespace=ISSUES_NAMESPACE,
        room=all_issues_room(),
    )
    await sio.emit(
        ISSUES_UPDATE_EVENT,
        [summary],
        namespace=ISSUES_NAMESPACE,
        room=repo_room(repo_full_name),
    )
    await sio.emit(
        ISSUE_DETAIL_UPDATE_EVENT,
        detail,
        namespace=ISSUES_NAMESPACE,
        room=issue_room(repo_full_name, issue_number),
    )
