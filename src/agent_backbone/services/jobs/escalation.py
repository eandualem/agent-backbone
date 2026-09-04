"""Escalation: stalls, dead sessions, and plans waiting for a human.

Nothing here restarts anything. The backbone reports; people (or the
escalation-target agent) decide.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from agent_backbone.models import DeliveryOutcome
from agent_backbone.recent import RecentKeys
from agent_backbone.services.agents import AgentState
from agent_backbone.services.integrations import notify_humans
from agent_backbone.services.routing import (
    format_offline_queue_notification,
    format_plan_notification,
    format_stall_notification,
    format_unexpected_offline_notification,
    list_open_queue_for_target,
    outcome_queues,
    safe_deliver,
)
from agent_backbone.services.terminal import query_format_vars

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.jobs.monitor import AgentStates

log = logging.getLogger(__name__)

_PLAN_NOTIFY_DEDUP_SECONDS = 1800
_escalated = RecentKeys(1800)
"""(session, event) pairs escalated within ``timing.escalation_dedup_seconds``."""
_plan_notified = RecentKeys(_PLAN_NOTIFY_DEDUP_SECONDS)
"""(session, plan ref) pairs the humans / escalation target were told about."""
_permission_notified = RecentKeys(_PLAN_NOTIFY_DEDUP_SECONDS)
"""(session, state timestamp) pairs of permission prompts the humans were told about."""


async def _attended(session: str) -> bool:
    """Whether someone is at that terminal (the tmux session is attached)."""
    try:
        vars_ = await query_format_vars(session, "attached=#{session_attached}")
    except Exception:
        return False
    return vars_.get("attached", "0") not in ("", "0")


def prompt_id(timestamp: float) -> str:
    """The identity of one prompt: the state's timestamp, as the button carries it."""
    return f"{timestamp:.3f}"


def permission_actions(
    config: BackboneConfig, agent: str, timestamp: float
) -> list[tuple[str, str]] | None:
    """Allow / Deny bound to *this* prompt: a button pressed after the agent
    moved on must not answer whatever is on screen then."""
    if not config.security.allow_remote_approval:
        return None
    ref = prompt_id(timestamp)
    return [("Allow", f"approve:{agent}:{ref}"), ("Deny", f"deny:{agent}:{ref}")]


def plan_actions(
    config: BackboneConfig, agent: str, timestamp: float
) -> list[tuple[str, str]] | None:
    if not config.security.allow_remote_plan_control:
        return None
    ref = prompt_id(timestamp)
    return [
        ("Approve plan", f"plan_approve:{agent}:{ref}"),
        ("Reject plan", f"plan_reject:{agent}:{ref}"),
    ]


async def check_permission_waiting(config: BackboneConfig, states: AgentStates) -> None:
    """Tell the humans about a permission prompt, with Allow / Deny buttons.

    Once per prompt (the state's timestamp), and not while someone is at
    that terminal: an attached tmux session means the dialog is being
    looked at. A ``question`` (a dialog the backbone cannot answer for a
    person) is reported without buttons.
    """
    for name, snapshot in states.items():
        if snapshot.state != AgentState.WAITING_FOR_HUMAN or snapshot.is_plan_waiting:
            continue
        key = (name, f"{snapshot.reason}:{snapshot.timestamp:.3f}")
        if _permission_notified.seen(key):
            continue
        if await _attended(name):
            continue
        if snapshot.reason == "permission":
            text = (
                f"\U0001f510 Permission prompt — {name}\n"
                "The runtime is asking to run a tool. Allow or deny it here, or in the "
                f"terminal: tmux attach -t {name}"
            )
            actions = permission_actions(config, name, snapshot.timestamp)
            if actions is None:
                text += (
                    "\n\nButtons are off: backbone config set security.allow_remote_approval true"
                )
        else:
            text = (
                f"\u2753 Question — {name}\n"
                f"The runtime is asking something only you can answer: tmux attach -t {name}"
            )
            actions = None
        if await notify_humans(config, text, agent=name, actions=actions):
            _permission_notified.mark(key)
            log.info("Sent permission-waiting notification for %s", name)


def _should_escalate(session: str, event_key: str, dedup_seconds: int) -> bool:
    return not _escalated.check_and_mark((session, event_key), ttl_seconds=dedup_seconds)


def _plan_notification_source_ref(
    *, channel: str, recipient: str, plan_file: str, plan_title: str, plan_timestamp: float
) -> str:
    plan_identity = plan_file or plan_title or "<untitled>"
    return f"{channel}:{recipient}:{plan_timestamp:.6f}:{plan_identity}"


def _plan_notification_already_sent(session_name: str, source_ref: str) -> bool:
    return _plan_notified.seen((session_name, source_ref))


def _record_plan_notification(session_name: str, source_ref: str) -> None:
    _plan_notified.mark((session_name, source_ref))


def _escalation_session(config: BackboneConfig, source_session: str) -> str | None:
    target = config.escalation.target
    if not target or target == source_session or target not in config.agents:
        return None
    return target


async def _pending_count_for_agent(
    name: str, config: BackboneConfig, gh: GitHubClient | None
) -> int:
    if gh is None:
        return 0
    return len(await list_open_queue_for_target(config, name, gh))


async def check_for_stalls(config: BackboneConfig, states: AgentStates) -> list[dict]:
    """Agents busy on one issue for longer than ``timing.stall_threshold_seconds``."""
    stalls: list[dict] = []
    threshold = config.timing.stall_threshold_seconds
    for name, snapshot in states.items():
        if snapshot.state != AgentState.BUSY or snapshot.current_issue is None:
            continue
        if snapshot.timestamp <= 0:
            continue
        duration = time.time() - snapshot.timestamp
        if duration >= threshold:
            stalls.append(
                {
                    "entity": name,
                    "session": name,
                    "issue_number": snapshot.current_issue,
                    "repo": snapshot.current_repo or "",
                    "duration_minutes": int(duration / 60),
                }
            )
    return stalls


async def check_for_unexpected_offline(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB, gh: GitHubClient | None
) -> list[dict]:
    """Known agents whose last recorded state was live but whose session is gone."""
    offline: list[dict] = []
    try:
        known_states = await db.states.all()
    except Exception:
        log.exception("Failed to read agent states for offline check")
        return offline

    for record in known_states:
        session_name = record["session_name"]
        if record.get("state", "unknown") == "unknown":
            continue
        if session_name in active_sessions or session_name not in config.agents:
            continue
        pending_count = 0
        try:
            pending_count = await _pending_count_for_agent(session_name, config, gh)
        except Exception:
            log.exception("Failed to count pending issues for %s", session_name)
        offline.append(
            {"entity": session_name, "session": session_name, "pending_count": pending_count}
        )
    return offline


async def handle_stalls(config: BackboneConfig, states: AgentStates, db: BackboneDB) -> None:
    """Detect stalled agents and escalate to the configured target."""
    for stall in await check_for_stalls(config, states):
        event_key = f"stall:{stall['repo']}#{stall['issue_number']}"
        if not _should_escalate(
            stall["session"], event_key, config.timing.escalation_dedup_seconds
        ):
            continue
        escalation_session = _escalation_session(config, stall["session"])
        if escalation_session and escalation_session in states:
            msg = format_stall_notification(
                stall["session"],
                stall["issue_number"] or 0,
                stall["duration_minutes"],
                stall["entity"],
            )
            await safe_deliver(
                escalation_session, msg, config, db=db, priority=True, delivery_kind="escalation"
            )
        log.warning(
            "Stall detected: %s on %s#%s (%dm)",
            stall["entity"],
            stall["repo"],
            stall["issue_number"],
            stall["duration_minutes"],
        )


async def handle_offline(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB, gh: GitHubClient | None
) -> None:
    """Report dead sessions (never restart them) and clear their recorded state.

    Agents are not expected to stay up. Only one marked ``always_on`` is
    reported the moment its session is gone; for every other agent the
    humans hear about it when messages are waiting for it
    (``report_offline_queues``).
    """
    for agent in await check_for_unexpected_offline(config, active_sessions, db, gh):
        spec = config.agents.get(agent["session"])
        expected_up = spec is not None and spec.always_on
        if expected_up and _should_escalate(
            agent["session"], "offline", config.timing.escalation_dedup_seconds
        ):
            escalation_session = _escalation_session(config, agent["session"])
            if escalation_session and escalation_session in active_sessions:
                msg = format_unexpected_offline_notification(
                    agent["session"], agent["entity"], agent["pending_count"]
                )
                await safe_deliver(
                    escalation_session,
                    msg,
                    config,
                    db=db,
                    priority=True,
                    delivery_kind="escalation",
                )
            await notify_humans(
                config,
                f"Agent {agent['entity']} went offline unexpectedly "
                f"({agent['pending_count']} pending). It was not restarted.",
                agent=agent["session"],
            )
            log.warning("Agent offline unexpectedly: %s", agent["entity"])
        elif not expected_up:
            log.info("Agent %s is offline (not always_on; not reported)", agent["entity"])
        try:
            await db.states.set(session_name=agent["session"], state="unknown", current_issue=None)
        except Exception:
            log.exception(
                "Failed to clear DB state for offline agent %s (non-fatal)", agent["session"]
            )
    await report_offline_queues(config, active_sessions, db)


async def report_offline_queues(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB
) -> None:
    """Tell the humans (and the escalation target) about messages waiting for
    an agent that is not running — the consequence of an absence, not the
    absence itself. ``always_on`` agents were already reported when they died."""
    for spec in config.agents:
        if spec.name in active_sessions or spec.always_on:
            continue
        try:
            queued = await db.queue.pending_count(spec.name)
        except Exception:
            log.exception("Failed to count queued messages for %s", spec.name)
            continue
        if queued == 0 or not _should_escalate(
            spec.name, "offline_queued", config.timing.escalation_dedup_seconds
        ):
            continue
        msg = format_offline_queue_notification(spec.name, queued)
        escalation_session = _escalation_session(config, spec.name)
        if escalation_session and escalation_session in active_sessions:
            await safe_deliver(
                escalation_session,
                msg,
                config,
                db=db,
                priority=True,
                delivery_kind="escalation",
            )
        word = "message" if queued == 1 else "messages"
        await notify_humans(
            config,
            f"Agent {spec.name} is offline with {queued} queued {word}. It was not restarted.",
            agent=spec.name,
        )
        log.info("Agent %s is offline with %d queued message(s)", spec.name, queued)


async def check_blocked(config: BackboneConfig, states: AgentStates) -> None:
    """Tell the humans once when an agent is blocked on its usage limit.

    Nothing to approve: the runtime resumes on its own. The message is
    what the runtime said (when the limit resets), deduplicated per agent
    for ``timing.escalation_dedup_seconds``."""
    for name, snapshot in states.items():
        if snapshot.state != AgentState.BLOCKED:
            continue
        if not _should_escalate(name, "blocked", config.timing.escalation_dedup_seconds):
            continue
        reason = snapshot.reason or "its runtime"
        detail = f" ({snapshot.detail})" if snapshot.detail else ""
        what = "its usage limit" if reason == "quota" else reason
        sent = await notify_humans(
            config,
            f"Agent {name} is blocked on {what}{detail}. It resumes on its own; "
            "messages for it are queued meanwhile.",
            agent=name,
        )
        if not sent:
            _escalated.forget((name, "blocked"))  # nobody heard it: try again next cycle
        log.info("Agent %s is blocked on %s%s", name, what, detail)


async def check_plan_waiting(
    config: BackboneConfig, states: AgentStates, db: BackboneDB | None = None
) -> None:
    """Tell the humans (every integration) and the escalation target about waiting plans."""
    for name, snapshot in states.items():
        if not snapshot.is_plan_waiting:
            continue

        plan_file = snapshot.plan_file or ""
        plan_title = snapshot.plan_title or "Untitled plan"
        plan_timestamp = snapshot.timestamp or 0.0

        human_ref = _plan_notification_source_ref(
            channel="integrations",
            recipient="humans",
            plan_file=plan_file,
            plan_title=plan_title,
            plan_timestamp=plan_timestamp,
        )
        if not _plan_notification_already_sent(name, human_ref):
            msg = (
                f"\U0001f4cb Plan waiting — {name}\nTitle: {plan_title}\n\n"
                f"/viewplan {name}\n/approve {name}"
            )
            if await notify_humans(
                config, msg, agent=name, actions=plan_actions(config, name, plan_timestamp)
            ):
                _record_plan_notification(name, human_ref)
                log.info("Sent plan-waiting notification for %s", name)

        escalation_session = _escalation_session(config, name)
        if escalation_session and escalation_session in states:
            orch_ref = _plan_notification_source_ref(
                channel="tmux",
                recipient=escalation_session,
                plan_file=plan_file,
                plan_title=plan_title,
                plan_timestamp=plan_timestamp,
            )
            if not _plan_notification_already_sent(name, orch_ref):
                orch_msg = format_plan_notification(
                    name, name, plan_file, plan_title, issue_number=snapshot.current_issue
                )
                outcome = await safe_deliver(
                    escalation_session,
                    orch_msg,
                    config,
                    db=db,
                    priority=True,
                    delivery_kind="escalation",
                )
                # A queued notification will reach the target when it frees up,
                # so record it either way and do not enqueue it again next run.
                if outcome == DeliveryOutcome.DELIVERED or outcome_queues(outcome, "escalation"):
                    _record_plan_notification(name, orch_ref)
                    log.info(
                        "Plan notification for %s -> %s (%s)", name, escalation_session, outcome
                    )
