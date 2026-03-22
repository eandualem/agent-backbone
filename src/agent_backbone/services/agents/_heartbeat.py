"""Heartbeat scheduler: evaluate cron/one-shot schedules and wake idle agents.

Reads heartbeat-schedules.json, evaluates each agent's schedule against the
current time, and delivers [via:heartbeat] tagged messages to idle tmux sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from agent_backbone.config import BackboneConfig
from agent_backbone.services._locator import get_config, get_db
from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.agents.models import AgentState
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.terminal import list_sessions, send_message

log = logging.getLogger(__name__)

# Concurrency guard — prevents parallel heartbeat runs
_heartbeat_lock = asyncio.Lock()


def load_schedules(path: Path) -> dict:
    """Read heartbeat schedules from JSON file.

    Returns {} on missing or malformed file.
    """
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_schedules(schedules: dict, path: Path) -> None:
    """Atomic write of schedules to JSON (write tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(schedules, indent=2))
    os.replace(tmp, path)


def is_due(schedule: dict, last_fired: str | None, default_tz: str) -> bool:
    """Determine if a heartbeat schedule is due for firing.

    For 'cron' entries: uses croniter to find the previous scheduled fire time.
    If last_fired is None or before the previous fire time, it's due.

    For 'at' entries: parses ISO 8601 datetime. Due if now >= at and not yet fired.
    """
    tz_name = schedule.get("timezone", default_tz)
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    if "cron" in schedule:
        expr = schedule["cron"]
        cron = croniter(expr, now)
        prev_fire = cron.get_prev(datetime)
        if not prev_fire.tzinfo:
            prev_fire = prev_fire.replace(tzinfo=tz)

        if last_fired is None:
            return True

        last_dt = datetime.fromisoformat(last_fired)
        if not last_dt.tzinfo:
            from datetime import UTC

            last_dt = last_dt.replace(tzinfo=UTC)

        return last_dt < prev_fire

    if "at" in schedule:
        at_dt = datetime.fromisoformat(schedule["at"])
        if not at_dt.tzinfo:
            at_dt = at_dt.replace(tzinfo=tz)

        if now < at_dt:
            return False

        return last_fired is None

    return False


def _schedule_targets(agent: str, config: BackboneConfig) -> list[str]:
    """Expand a heartbeat schedule key to concrete runtime sessions."""
    sessions = config.registry.resolve_entity_sessions(agent)
    if sessions:
        return sessions
    if agent not in config.registry.entities:
        return [agent]
    log.warning("Registry entity %s has no concrete sessions", agent)
    return []


async def evaluate_agent_heartbeat(
    agent: str,
    schedule: dict,
    active_sessions: set[str],
    config: BackboneConfig,
    db: BackboneDB,
) -> str | None:
    """Evaluate and potentially deliver a heartbeat for a single agent.

    Returns outcome string: "delivered", "skipped_busy", "skipped_offline",
    "skipped_disabled", "not_due", or None.
    """
    if not schedule.get("enabled", True):
        return "skipped_disabled"

    # Resolve session name
    session = config.registry.sessions_map.get(agent, agent)

    # Check if due — need last fired from SQLite
    last_fired = await db.get_last_heartbeat(agent)

    if not is_due(schedule, last_fired, config.heartbeat.default_timezone):
        return "not_due"

    # Check agent state — only deliver to idle
    snapshot = await get_agent_state(
        config.agent_state.state_path,
        session,
        config.agent_state.stale_threshold_seconds,
    )
    if snapshot.state != AgentState.IDLE:
        log.info("Heartbeat skipped for %s — state=%s", agent, snapshot.state.value)
        await db.record_heartbeat(agent, "skipped_busy")
        return "skipped_busy"

    # Check tmux session exists
    if session not in active_sessions:
        log.info("Heartbeat skipped for %s — session %s offline", agent, session)
        await db.record_heartbeat(agent, "skipped_offline")
        return "skipped_offline"

    # Format and deliver
    tz_name = schedule.get("timezone", config.heartbeat.default_timezone)
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    message = (
        f"[via:heartbeat] {timestamp} {tz_name} — Heartbeat. Check ROUTINE.md and HEARTBEAT.md."
    )

    if await send_message(session, message):
        await db.record_heartbeat(agent, "delivered", message)
        log.info("Heartbeat delivered to %s (session: %s)", agent, session)
        return "delivered"

    return None


async def heartbeat_scheduler() -> dict:
    """Evaluate heartbeat schedules and wake agents at configured times.

    Returns a dict mapping agent → outcome.
    """
    if _heartbeat_lock.locked():
        log.info("Heartbeat scheduler already running — skipping concurrent run")
        return {"_skipped": "concurrent_run"}

    async with _heartbeat_lock:
        return await _heartbeat_scheduler_impl()


async def _heartbeat_scheduler_impl() -> dict:
    """Inner implementation of heartbeat_scheduler, guarded by _heartbeat_lock."""
    result: dict[str, str] = {}
    config = get_config()
    db = get_db()

    schedules = load_schedules(config.heartbeat.schedule_path)
    if not schedules:
        log.info("No heartbeat schedules found")
        return result

    active_sessions = set(await list_sessions())
    one_shots_fired: list[str] = []

    for agent, schedule in schedules.items():
        targets = _schedule_targets(agent, config)
        if not targets:
            log.info("Heartbeat skipped for %s — no deliverable targets", agent)
            continue
        target_outcomes: list[str] = []
        for target in targets:
            outcome = await evaluate_agent_heartbeat(
                agent=target,
                schedule=schedule,
                active_sessions=active_sessions,
                config=config,
                db=db,
            )
            if outcome:
                result[target] = outcome
                target_outcomes.append(outcome)

        # Track one-shot entries that fired
        if target_outcomes and all(outcome == "delivered" for outcome in target_outcomes):
            if schedule.get("one_shot"):
                one_shots_fired.append(agent)

    # Remove fired one-shot entries and save
    if one_shots_fired:
        for agent in one_shots_fired:
            del schedules[agent]
        save_schedules(schedules, config.heartbeat.schedule_path)
        log.info("Removed one-shot entries: %s", one_shots_fired)

    return result
