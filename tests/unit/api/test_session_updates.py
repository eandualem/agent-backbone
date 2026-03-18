"""Tests for api/session_updates.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.api.models import EnrichedAgent
from agent_backbone.api.session_updates import (
    SESSIONS_NAMESPACE,
    SESSIONS_UPDATE_EVENT,
    emit_sessions_update,
    reset_sessions_update_state,
)


@pytest.fixture(autouse=True)
def _reset_update_state():
    reset_sessions_update_state()
    yield
    reset_sessions_update_state()


def _sample_snapshot() -> list[EnrichedAgent]:
    return [
        EnrichedAgent(
            session="agent-backbone",
            entity="agent-backbone",
            state="idle",
            online=True,
            type="coding_agent",
        )
    ]


class TestEmitSessionsUpdate:
    @pytest.mark.asyncio
    async def test_noops_without_services(self):
        """Missing server or services skips broadcasting cleanly."""
        assert await emit_sessions_update(None, MagicMock(), MagicMock(), MagicMock()) is False
        assert await emit_sessions_update(MagicMock(), MagicMock(), None, MagicMock()) is False
        assert await emit_sessions_update(MagicMock(), MagicMock(), MagicMock(), None) is False

    @pytest.mark.asyncio
    async def test_emits_snapshot_payload(self):
        """Broadcast emits the canonical event on the sessions namespace."""
        sio = MagicMock()
        sio.emit = AsyncMock()

        with (
            patch(
                "agent_backbone.api.session_updates.invalidate_session_snapshot_caches"
            ) as mock_invalidate,
            patch(
                "agent_backbone.api.session_updates.build_session_snapshot",
                new_callable=AsyncMock,
                return_value=_sample_snapshot(),
            ),
        ):
            emitted = await emit_sessions_update(sio, MagicMock(), MagicMock(), MagicMock())

        assert emitted is True
        mock_invalidate.assert_called_once_with()
        sio.emit.assert_awaited_once_with(
            SESSIONS_UPDATE_EVENT,
            [_sample_snapshot()[0].model_dump(mode="json")],
            namespace=SESSIONS_NAMESPACE,
        )

    @pytest.mark.asyncio
    async def test_only_if_changed_suppresses_duplicate_payload(self):
        """Watcher mode emits only when the snapshot payload actually changes."""
        sio = MagicMock()
        sio.emit = AsyncMock()

        with (
            patch(
                "agent_backbone.api.session_updates.invalidate_session_snapshot_caches"
            ),
            patch(
                "agent_backbone.api.session_updates.build_session_snapshot",
                new_callable=AsyncMock,
                return_value=_sample_snapshot(),
            ),
        ):
            first = await emit_sessions_update(
                sio,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                only_if_changed=True,
            )
            second = await emit_sessions_update(
                sio,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                only_if_changed=True,
            )

        assert first is True
        assert second is False
        sio.emit.assert_awaited_once()
