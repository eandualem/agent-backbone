"""Workflow execution engine for JSON-defined workflows.

Executes multi-step workflows with start/stop/message actions against
tmux sessions. Continues execution on step failure — partial success
is valuable information.
"""

from __future__ import annotations

import logging

from agent_backbone.config import BackboneConfig
from agent_backbone.services.tmux import (
    resolve_agent_dir,
    send_message,
    start_session,
    stop_session,
)

log = logging.getLogger(__name__)


async def execute_workflow_steps(
    steps: list[dict],
    config: BackboneConfig,
) -> dict:
    """Execute a sequence of workflow steps.

    Each step must have an 'action' and 'session' key. Supported actions:
    - start: Start a session (resolves working dir, launches claude CLI)
    - stop: Stop a session
    - message: Send a message to a session

    Continues on step failure. Overall ok is False if any step failed.

    Returns:
        {"ok": bool, "steps": [{"action": str, "session": str, "ok": bool, "detail": str}]}
    """
    results: list[dict] = []
    all_ok = True

    for step in steps:
        action = step.get("action", "")
        session = step.get("session", "")

        if not action or not session:
            results.append(
                {
                    "action": action or "unknown",
                    "session": session or "unknown",
                    "ok": False,
                    "detail": "Step missing required 'action' or 'session' field",
                }
            )
            all_ok = False
            continue

        try:
            if action == "start":
                working_dir = step.get("working_dir") or resolve_agent_dir(session)
                command_str = step.get("command", "claude")
                ok = await start_session(
                    session,
                    working_dir=working_dir or None,
                    command=[command_str] if command_str else None,
                )
                detail = "started" if ok else "failed to start"
            elif action == "stop":
                ok = await stop_session(session)
                detail = "stopped" if ok else "failed to stop"
            elif action == "message":
                message = step.get("message", "")
                if not message:
                    ok = False
                    detail = "No message provided"
                else:
                    ok = await send_message(session, message)
                    detail = "sent" if ok else "failed to send"
            else:
                ok = False
                detail = f"Unknown action: {action}"
        except Exception as exc:
            ok = False
            detail = str(exc)
            log.exception("Workflow step failed: %s %s", action, session)

        results.append(
            {
                "action": action,
                "session": session,
                "ok": ok,
                "detail": detail,
            }
        )
        if not ok:
            all_ok = False

    return {"ok": all_ok, "steps": results}
