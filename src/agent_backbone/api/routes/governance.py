"""Governance action execution — frontend governance engine triggers actions via the backbone."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Request

from agent_backbone.api.deps import get_config, get_db, get_github
from agent_backbone.api.models import GovernanceActionRequest, GovernanceActionResponse
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/governance", tags=["governance"])


async def _handle_notify_agent(
    params: dict[str, Any],
    config: BackboneConfig,
    db: BackboneDB,
) -> dict[str, Any]:
    from agent_backbone.services.routing._delivery import safe_deliver

    outcome = await safe_deliver(
        session_name=params["session"],
        message=params["message"],
        config=config,
        db=db,
        delivery_kind="direct_message",
    )
    return {"outcome": outcome}


async def _handle_notify_backbone(
    params: dict[str, Any],
    config: BackboneConfig,
    db: BackboneDB,
) -> dict[str, Any]:
    from agent_backbone.services.routing._delivery import safe_deliver

    outcome = await safe_deliver(
        session_name=params["session"],
        message=f"[via:governance] {params['message']}",
        config=config,
        db=db,
        delivery_kind="direct_message",
    )
    return {"outcome": outcome}


async def _handle_notify_elias(
    params: dict[str, Any],
    config: BackboneConfig,
) -> dict[str, Any]:
    from agent_backbone.services.telegram.interface import TelegramService

    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = config.telegram.notification_chat_id
    result = await TelegramService.send_notification(telegram_token, chat_id, params["message"])
    return {"sent": result}


async def _handle_notify_jarvis(
    params: dict[str, Any],
    config: BackboneConfig,
) -> dict[str, Any]:
    from agent_backbone.jarvis import inject_message

    result = await inject_message(
        config.jarvis.inject_url,
        params["message"],
        sessions_url=config.jarvis.sessions_url,
    )
    return {"sent": result}


async def _handle_auto_comment(
    params: dict[str, Any],
    gh: GitHubClient,
) -> dict[str, Any]:
    await gh.add_comment(
        params["issue_number"],
        params["body"],
        repo_full_name=params.get("repo"),
    )
    return {"commented": True}


async def _handle_log(params: dict[str, Any]) -> dict[str, Any]:
    log.info("Governance log action: %s", params.get("message", ""))
    return {"logged": True}


async def _handle_semantic_search() -> dict[str, Any]:
    return {"stub": True, "message": "semantic_search not yet implemented"}


@router.post("/actions", response_model=GovernanceActionResponse)
async def execute_governance_action(
    body: GovernanceActionRequest,
    request: Request,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    gh: GitHubClient = Depends(get_github),
) -> GovernanceActionResponse:
    """Execute a governance action dispatched by the frontend governance engine."""
    action_type = body.action_type
    params = body.params

    handlers = {
        "notify_agent": lambda: _handle_notify_agent(params, config, db),
        "notify_backbone": lambda: _handle_notify_backbone(params, config, db),
        "notify_elias": lambda: _handle_notify_elias(params, config),
        "notify_jarvis": lambda: _handle_notify_jarvis(params, config),
        "auto_comment": lambda: _handle_auto_comment(params, gh),
        "log": lambda: _handle_log(params),
        "semantic_search": lambda: _handle_semantic_search(),
    }

    handler = handlers.get(action_type)
    if handler is None:
        return GovernanceActionResponse(
            ok=False,
            action_type=action_type,
            result={"error": f"Unknown action type: {action_type}"},
        )

    try:
        result = await handler()
    except Exception as exc:
        log.warning("[GOVERNANCE] Action %s failed: %s", action_type, exc, exc_info=True)
        return GovernanceActionResponse(
            ok=False,
            action_type=action_type,
            result={"error": str(exc)},
        )

    # Emit governance event after successful execution
    from agent_backbone.api.governance_events import emit_governance_event

    await emit_governance_event(
        "action.executed",
        context=body.track_context,
        source="governance",
        data={"action_type": action_type, "result": result},
        sio=getattr(request.app.state, "sio", None),
    )

    return GovernanceActionResponse(ok=True, action_type=action_type, result=result)
