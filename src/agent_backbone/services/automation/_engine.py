"""Workflow execution engine for JSON-defined workflows.

Executes multi-step workflows with start/stop/message actions against
tmux sessions. Continues execution on step failure — partial success
is valuable information.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_backbone.config import BackboneConfig
from agent_backbone.services.terminal import (
    RUNTIME_ENV_KEY,
    resolve_agent_dir,
    send_message,
    start_session,
    stop_session,
)

log = logging.getLogger(__name__)


def _step_result(action: str, session: str, ok: bool, detail: str) -> dict[str, Any]:
    """Build the stable workflow step response payload."""
    return {
        "action": action,
        "session": session,
        "ok": ok,
        "detail": detail,
    }


def _workflow_working_dir(session: str, config: BackboneConfig) -> str:
    """Resolve a workflow step session to a concrete workspace directory."""
    working_dir = resolve_agent_dir(session)
    if working_dir:
        return working_dir
    return resolve_agent_dir(session, config.registry)


async def _execute_start_step(step: dict[str, Any], config: BackboneConfig) -> tuple[bool, str]:
    """Execute a workflow start action."""
    session = step["session"]
    working_dir = step.get("working_dir") or _workflow_working_dir(session, config)
    command_str = step.get("command", "claude")
    command = [command_str] if command_str else None
    environment = {RUNTIME_ENV_KEY: command[0]} if command else None
    ok = await start_session(
        session,
        working_dir=working_dir or None,
        command=command,
        environment=environment,
    )
    return ok, "started" if ok else "failed to start"


async def _execute_stop_step(step: dict[str, Any], _config: BackboneConfig) -> tuple[bool, str]:
    """Execute a workflow stop action."""
    ok = await stop_session(step["session"])
    return ok, "stopped" if ok else "failed to stop"


async def _execute_message_step(
    step: dict[str, Any], _config: BackboneConfig
) -> tuple[bool, str]:
    """Execute a workflow message action."""
    message = step.get("message", "")
    if not message:
        return False, "No message provided"

    ok = await send_message(step["session"], message)
    return ok, "sent" if ok else "failed to send"


_ACTION_HANDLERS = {
    "start": _execute_start_step,
    "stop": _execute_stop_step,
    "message": _execute_message_step,
}


def _validate_step(step: dict[str, Any]) -> tuple[str, str] | None:
    """Validate required step fields and return the normalized identifiers."""
    action = step.get("action", "")
    session = step.get("session", "")
    if action and session:
        return action, session
    return None


async def _execute_step(step: dict[str, Any], config: BackboneConfig) -> dict[str, Any]:
    """Execute a single workflow step and return the stable result payload."""
    validated = _validate_step(step)
    if validated is None:
        action = step.get("action", "") or "unknown"
        session = step.get("session", "") or "unknown"
        return _step_result(
            action,
            session,
            False,
            "Step missing required 'action' or 'session' field",
        )

    action, session = validated
    handler = _ACTION_HANDLERS.get(action)
    if handler is None:
        return _step_result(action, session, False, f"Unknown action: {action}")

    try:
        ok, detail = await handler(step, config)
    except Exception as exc:
        log.exception("Workflow step failed: %s %s", action, session)
        return _step_result(action, session, False, str(exc))

    return _step_result(action, session, ok, detail)


async def execute_workflow_steps(steps: list[dict], config: BackboneConfig) -> dict:
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
        result = await _execute_step(step, config)
        results.append(result)
        if not result["ok"]:
            all_ok = False

    return {"ok": all_ok, "steps": results}
