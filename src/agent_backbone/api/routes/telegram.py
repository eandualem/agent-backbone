"""Telegram endpoints — route agent replies back to their Telegram topic."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config
from agent_backbone.api.models import TelegramReplyRequest
from agent_backbone.config import BackboneConfig
from agent_backbone.services.telegram._topic_discovery import (
    CATCH_ALL_TOPIC,
    effective_group_chat_id,
    effective_routes,
    load_discovery,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["telegram"])


def _reverse_topic_routes(routes: dict[int, str]) -> dict[str, int]:
    """Build session_name → thread_id mapping from topic_routes."""
    return {
        session: thread_id for thread_id, session in routes.items() if session != CATCH_ALL_TOPIC
    }


async def _send_telegram_reply(token: str, group_chat_id: int, thread_id: int, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": group_chat_id, "message_thread_id": thread_id, "text": text}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            return resp.status_code == 200
    except Exception:
        log.exception("Telegram sendMessage failed")
        return False


@router.post("/telegram/reply")
async def handle_reply(
    body: TelegramReplyRequest,
    config: BackboneConfig = Depends(get_config),
):
    """Post an agent's reply into the Telegram topic mapped to its session."""
    if not body.session or not body.text:
        raise HTTPException(status_code=400, detail="session and text required")
    if not config.telegram_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_TOKEN not set")

    discovery = load_discovery(config.telegram_topic_discovery_path)
    group_chat_id = effective_group_chat_id(config, discovery)
    if not group_chat_id:
        raise HTTPException(status_code=503, detail="group_chat_id not configured")

    thread_id = _reverse_topic_routes(effective_routes(config, discovery)).get(body.session)
    if not thread_id:
        raise HTTPException(status_code=404, detail=f"no topic route for session '{body.session}'")

    ok = await _send_telegram_reply(config.telegram_token, group_chat_id, thread_id, body.text)
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram API call failed")
    return {"ok": True, "session": body.session}
