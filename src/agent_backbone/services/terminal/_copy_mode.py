"""Immediate copy-mode recovery for tmux-backed sessions."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from agent_backbone.config import BackboneConfig
from agent_backbone.services.terminal._adapters import get_terminal_adapter_for_session
from agent_backbone.services.terminal._sessions import query_format_vars

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_COPY_MODE_RECHECK_DELAY_SECONDS = 0.1
_COPY_MODE_ALERT_DEDUP_SECONDS = 1800
_copy_mode_alerted_at: dict[str, float] = {}
_last_non_copy_state: dict[str, str] = {}


class TelegramService:
    """Lazy proxy to avoid importing telegram service during module import."""

    @staticmethod
    async def send_notification(*args, **kwargs):
        from agent_backbone.services.telegram.interface import TelegramService as _TelegramService

        return await _TelegramService.send_notification(*args, **kwargs)


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
    for session in list(_copy_mode_alerted_at):
        if session not in managed_sessions:
            del _copy_mode_alerted_at[session]
    for session in list(_last_non_copy_state):
        if session not in managed_sessions:
            del _last_non_copy_state[session]


def _copy_mode_alert_message(session_name: str, preceded_by: str) -> str:
    """Human-readable Telegram alert for a stuck copy-mode session."""
    return (
        f"Copy mode persists in {session_name}\n"
        f"Preceded by: {preceded_by}\n"
        "Backbone immediately sent q, but the pane is still in copy mode."
    )


async def handle_copy_mode_recovery(
    config: BackboneConfig,
    active_sessions: set[str],
    db: BackboneDB | None = None,
) -> None:
    """Detect accidental copy mode and immediately attempt recovery."""
    managed_sessions = set(_managed_sessions(config, active_sessions))
    _clear_stale_sessions(managed_sessions)

    if not managed_sessions:
        return

    notification_chat_id = config.telegram.notification_chat_id
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
    if db is None:
        try:
            from agent_backbone.services._locator import get_db

            db = get_db()
        except RuntimeError:
            db = None
    from agent_backbone.services.agents.interface import StateService

    state_svc = StateService(db=db)

    for session_name in sorted(managed_sessions):
        snapshot = await state_svc.get_state(session_name)
        adapter = await get_terminal_adapter_for_session(session_name)
        tmux_vars = await query_format_vars(session_name, "pane_in_mode=#{pane_in_mode}")
        in_copy_mode = adapter.detect_copy_mode(tmux_vars, snapshot.state)

        if not in_copy_mode:
            _last_non_copy_state[session_name] = snapshot.state.value
            _copy_mode_alerted_at.pop(session_name, None)
            continue

        preceded_by = _last_non_copy_state.get(session_name, snapshot.state.value)
        log.warning(
            "Copy mode detected in %s via %s adapter (preceded_by=%s, agent_state=%s)",
            session_name,
            adapter.runtime.value,
            preceded_by,
            snapshot.state.value,
        )

        exited = await adapter.exit_copy_mode(session_name)
        if exited:
            await asyncio.sleep(_COPY_MODE_RECHECK_DELAY_SECONDS)

        tmux_vars = await query_format_vars(session_name, "pane_in_mode=#{pane_in_mode}")
        if not adapter.detect_copy_mode(tmux_vars, snapshot.state):
            _last_non_copy_state[session_name] = snapshot.state.value
            _copy_mode_alerted_at.pop(session_name, None)
            log.info(
                "Copy mode cleared for %s via %s adapter",
                session_name,
                adapter.runtime.value,
            )
            continue

        last_alert = _copy_mode_alerted_at.get(session_name)
        if (
            last_alert is not None
            and (time.monotonic() - last_alert) < _COPY_MODE_ALERT_DEDUP_SECONDS
        ):
            continue

        _copy_mode_alerted_at[session_name] = time.monotonic()
        if notification_chat_id and telegram_token:
            await TelegramService.send_notification(
                telegram_token,
                notification_chat_id,
                _copy_mode_alert_message(session_name, preceded_by),
            )
        else:
            log.warning(
                "Copy mode persists in %s after immediate exit attempt (preceded_by=%s)",
                session_name,
                preceded_by,
            )
