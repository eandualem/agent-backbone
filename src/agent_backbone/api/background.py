"""Background alignment verifier for agent state."""

from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _report_divergence(
    *,
    db,
    entity: str,
    session: str,
    reported,
    observed,
    interval: int,
) -> None:
    """Log and record a state divergence for monitoring visibility."""
    from agent_backbone.api.activity_events import record_activity_event

    payload = {
        "reported_state": reported.state.value,
        "observed_state": observed.state.value,
        "reported_context": reported.context,
        "observed_context": observed.context,
    }
    mismatch_message = (
        "State alignment mismatch for %s: reported=%s observed=%s "
        "reported_context=%s observed_context=%s"
    )
    log.warning(
        mismatch_message,
        session,
        reported.state.value,
        observed.state.value,
        (
            json.dumps(reported.context, sort_keys=True)
            if isinstance(reported.context, dict)
            else reported.context
        ),
        (
            json.dumps(observed.context, sort_keys=True)
            if isinstance(observed.context, dict)
            else observed.context
        ),
    )

    now = time.time()
    source_ref = f"{reported.state.value}->{observed.state.value}"
    already_reported = await db.has_activity_event(
        session=session,
        event="agent.divergence_detected",
        source_ref=source_ref,
        since=now - max(300, interval * 2),
    )
    if already_reported:
        return

    await record_activity_event(
        db,
        session=session,
        event_type="agent.divergence_detected",
        entity=entity,
        data=payload,
        source_ref=source_ref,
        ts=now,
    )


async def _poll_loop(interval: int = 30) -> None:
    """Verify DB-reported state against tmux/process observations."""
    from agent_backbone.services._locator import get_config, get_db
    from agent_backbone.services.agents import AgentState
    from agent_backbone.services.agents._observation import snapshot_from_observation
    from agent_backbone.services.agents.interface import StateService

    while True:
        try:
            await asyncio.sleep(interval)
            config = get_config()
            db = get_db()
            state_svc = StateService(db=db)
            sessions = set(config.registry.sessions_map.values()) | set(config.registry.repo_names)
            for session in sorted(s for s in sessions if s):
                entity = config.registry.entity_by_session.get(session, session)
                reported = await state_svc.get_reported_state(session)
                observed = await state_svc.observe_session(session)
                if not observed.online and reported.state != AgentState.OFFLINE:
                    await _report_divergence(
                        db=db,
                        entity=entity,
                        session=session,
                        reported=reported,
                        observed=snapshot_from_observation(observed),
                        interval=interval,
                    )
                    continue
                if observed.online and reported.state == AgentState.OFFLINE:
                    await _report_divergence(
                        db=db,
                        entity=entity,
                        session=session,
                        reported=reported,
                        observed=snapshot_from_observation(observed),
                        interval=interval,
                    )
                    continue
                if (
                    observed.online
                    and reported.state
                    in {
                        AgentState.IDLE,
                        AgentState.SUB_AGENT_WAITING,
                        AgentState.PERMISSION_WAITING,
                    }
                ):
                    observed_snapshot = snapshot_from_observation(observed)
                    if reported.state != observed_snapshot.state:
                        await _report_divergence(
                            db=db,
                            entity=entity,
                            session=session,
                            reported=reported,
                            observed=observed_snapshot,
                            interval=interval,
                        )
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("State alignment verification failed (will retry)")


def start_background_tasks() -> None:
    """Start all background tasks. Called during gateway lifespan."""
    global _task  # noqa: PLW0603
    _task = asyncio.create_task(_poll_loop(), name="bg-state-alignment-verifier")
    log.info("Background state alignment verifier started (30s interval)")


async def stop_background_tasks() -> None:
    """Cancel all background tasks. Called during gateway shutdown."""
    global _task  # noqa: PLW0603
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    log.info("Background tasks stopped")
