"""Copy-mode auto-recovery for tmux-backed agent sessions."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents.interface import StateService
from agent_backbone.services.agents.models import AgentState
from agent_backbone.services.terminal import query_format_vars, send_keys

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_COPY_MODE_RECOVERY_AFTER_SECONDS = 30.0
_COPY_MODE_ALERT_AFTER_ATTEMPT_SECONDS = 120.0
_WORKING_STATES = frozenset({AgentState.BUSY, AgentState.STARTING})


@dataclass
class CopyModeIncident:
    """Lifecycle state for a single copy-mode incident."""

    first_seen_at: float
    preceded_by: str
    auto_exit_attempted_at: float | None = None
    alert_handled_at: float | None = None


_copy_mode_incidents: dict[str, CopyModeIncident] = {}
_last_non_copy_state: dict[str, str] = {}


class TelegramService:
    """Lazy proxy to avoid importing telegram service during module import."""

    @staticmethod
    async def send_notification(*args, **kwargs):
        from agent_backbone.services.telegram.interface import TelegramService as _TelegramService

        return await _TelegramService.send_notification(*args, **kwargs)


async def get_agent_state(
    state_dir: object,
    session: str,
    stale_threshold: float = 300.0,
):
    """Compatibility shim for legacy callers patched at this module boundary."""
    del state_dir, stale_threshold
    db: BackboneDB | None = None
    try:
        from agent_backbone.services._locator import get_db

        db = get_db()
    except RuntimeError:
        pass
    return await StateService(db=db).get_state(session)


def _managed_sessions(config: BackboneConfig, active_sessions: set[str]) -> list[str]:
    """Active non-service sessions managed by backbone."""
    repo_names_lower = {name.lower() for name in config.registry.repo_names}
    known_sessions = set(config.registry.entity_by_session)
    return sorted(
        session
        for session in active_sessions
        if session not in config.entities.service_sessions
        and (session in known_sessions or session.lower() in repo_names_lower)
    )


def _clear_stale_sessions(managed_sessions: set[str]) -> None:
    """Forget sessions that are no longer active or backbone-managed."""
    for session in list(_copy_mode_incidents):
        if session not in managed_sessions:
            del _copy_mode_incidents[session]
    for session in list(_last_non_copy_state):
        if session not in managed_sessions:
            del _last_non_copy_state[session]


def _copy_mode_alert_message(
    session_name: str,
    duration_seconds: float,
    incident: CopyModeIncident,
    since_attempt_seconds: float,
) -> str:
    """Human-readable Telegram alert for a stuck copy-mode session."""
    return (
        f"Copy mode stuck — {session_name}\n"
        f"Duration: {int(duration_seconds)}s\n"
        f"Preceded by: {incident.preceded_by}\n"
        f"Auto-exit attempted {int(since_attempt_seconds)}s ago.\n"
        "Backbone sent q but the pane is still in copy mode."
    )


async def _pane_in_copy_mode(session_name: str, agent_state: AgentState) -> bool:
    """Whether tmux currently reports this session as blocked by copy mode."""
    try:
        tmux_vars = await query_format_vars(session_name, "pane_in_mode=#{pane_in_mode}")
    except Exception:
        log.debug("Failed to query copy-mode vars for '%s' (non-fatal)", session_name)
        return False

    return tmux_vars.get("pane_in_mode") == "1" and agent_state not in _WORKING_STATES


async def handle_copy_mode_recovery(
    config: BackboneConfig,
    active_sessions: set[str],
    db: BackboneDB | None = None,
) -> None:
    """Detect accidental copy mode, auto-exit it, and alert if it persists."""
    managed_sessions = set(_managed_sessions(config, active_sessions))
    _clear_stale_sessions(managed_sessions)

    if not managed_sessions:
        return

    now = time.monotonic()
    notification_chat_id = config.telegram.notification_chat_id
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
    if db is None:
        try:
            from agent_backbone.services._locator import get_db

            db = get_db()
        except RuntimeError:
            db = None
    for session_name in sorted(managed_sessions):
        snapshot = await get_agent_state(
            config.agent_state.state_path,
            session_name,
            config.agent_state.stale_threshold_seconds,
        )
        in_copy_mode = await _pane_in_copy_mode(session_name, snapshot.state)

        if not in_copy_mode:
            incident = _copy_mode_incidents.pop(session_name, None)
            if incident is not None:
                duration = now - incident.first_seen_at
                log.info(
                    "Copy mode cleared for %s after %.0fs (preceded_by=%s, auto_exit=%s)",
                    session_name,
                    duration,
                    incident.preceded_by,
                    incident.auto_exit_attempted_at is not None,
                )
            _last_non_copy_state[session_name] = snapshot.state.value
            continue

        incident = _copy_mode_incidents.get(session_name)
        if incident is None:
            incident = CopyModeIncident(
                first_seen_at=now,
                preceded_by=_last_non_copy_state.get(session_name, snapshot.state.value),
            )
            _copy_mode_incidents[session_name] = incident
            log.warning(
                "Copy mode detected in %s (preceded_by=%s, agent_state=%s)",
                session_name,
                incident.preceded_by,
                snapshot.state.value,
            )
            continue

        duration = now - incident.first_seen_at
        if (
            incident.auto_exit_attempted_at is None
            and duration >= _COPY_MODE_RECOVERY_AFTER_SECONDS
        ):
            sent = await send_keys(session_name, "q")
            incident.auto_exit_attempted_at = now
            if sent:
                log.warning(
                    "Attempted copy-mode recovery for %s after %.0fs (preceded_by=%s)",
                    session_name,
                    duration,
                    incident.preceded_by,
                )
                if not await _pane_in_copy_mode(session_name, snapshot.state):
                    _copy_mode_incidents.pop(session_name, None)
                    _last_non_copy_state[session_name] = snapshot.state.value
                    log.info(
                        "Copy mode recovered immediately for %s after %.0fs",
                        session_name,
                        duration,
                    )
                    continue
            else:
                log.warning(
                    "Failed to send copy-mode recovery key to %s after %.0fs (preceded_by=%s)",
                    session_name,
                    duration,
                    incident.preceded_by,
                )

        if incident.auto_exit_attempted_at is None or incident.alert_handled_at is not None:
            continue

        since_attempt = now - incident.auto_exit_attempted_at
        if since_attempt < _COPY_MODE_ALERT_AFTER_ATTEMPT_SECONDS:
            continue

        if notification_chat_id and telegram_token:
            sent = await TelegramService.send_notification(
                telegram_token,
                notification_chat_id,
                _copy_mode_alert_message(session_name, duration, incident, since_attempt),
            )
            if sent:
                incident.alert_handled_at = now
                log.warning(
                    "Sent copy-mode persistence alert for %s (duration=%.0fs, preceded_by=%s)",
                    session_name,
                    duration,
                    incident.preceded_by,
                )
        else:
            incident.alert_handled_at = now
            log.warning(
                "Copy mode persists in %s for %.0fs after auto-exit attempt "
                "(preceded_by=%s, Telegram not configured)",
                session_name,
                duration,
                incident.preceded_by,
            )
