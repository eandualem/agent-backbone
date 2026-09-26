"""Escalation: stalls, dead sessions, and plans waiting for a human.

Nothing here restarts anything. The backbone reports; people (or the
escalation-target agent) decide.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from agent_backbone.models import DeliveryOutcome
from agent_backbone.recent import RecentKeys
from agent_backbone.services.agents import AgentState, prompt_id
from agent_backbone.services.integrations import notify_humans
from agent_backbone.services.routing import (
    format_offline_queue_notification,
    format_plan_notification,
    format_stall_notification,
    format_unexpected_offline_notification,
    list_open_queue_for_target,
    safe_deliver,
)
from agent_backbone.services.runtimes import Runtime, get_runtime, resolve_runtime
from agent_backbone.services.terminal import capture_pane, query_format_vars

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


def permission_actions(
    config: BackboneConfig, agent: str, ref: str
) -> list[tuple[str, str]] | None:
    """Allow / Deny bound to *this* prompt: a button pressed after the agent
    moved on must not answer whatever is on screen then."""
    if not config.security.allow_remote_approval:
        return None
    return [("Allow", f"approve:{agent}:{ref}"), ("Deny", f"deny:{agent}:{ref}")]


def plan_actions(config: BackboneConfig, agent: str, ref: str) -> list[tuple[str, str]] | None:
    if not config.security.allow_remote_plan_control:
        return None
    return [
        ("Approve plan", f"plan_approve:{agent}:{ref}"),
        ("Reject plan", f"plan_reject:{agent}:{ref}"),
    ]


async def _dialog_text(config: BackboneConfig, name: str) -> str:
    """What the agent's dialog asks, for the person deciding from their phone."""
    spec = config.agents.get(name)
    try:
        pane = await capture_pane(name, lines=40)
        if not pane:
            return ""
        rt = await resolve_runtime(name, hint=spec.runtime if spec else None, pane_content=pane)
        if not rt.detect_active_dialog(pane):
            return ""  # a dialog left above an idle prompt is history, not the ask
        return rt.dialog_summary(pane)
    except Exception:
        log.debug("Could not read %s's dialog for the notification", name)
        return ""


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
        key = (name, prompt_id(snapshot))
        if _permission_notified.seen(key):
            continue
        if await _attended(name):
            continue
        # The dialog's own words (the command, the runtime's reason), so the
        # person can see what they are answering; runtime output, previewed.
        asked = await _dialog_text(config, name)
        if snapshot.reason == "permission":
            text = (
                f"\U0001f510 Permission prompt — {name}\n"
                + (f"{asked}\n" if asked else "The runtime is asking to run a tool. ")
                + f"Allow or deny it here, or in the terminal: tmux attach -t {name}"
            )
            actions = permission_actions(config, name, prompt_id(snapshot))
            if actions is None:
                text += (
                    "\n\nButtons are off: backbone config set security.allow_remote_approval true"
                )
        else:
            text = (
                f"\u2753 Question — {name}\n"
                + (f"{asked}\n" if asked else "")
                + f"The runtime is asking something only you can answer: tmux attach -t {name}"
            )
            actions = None
        if await notify_humans(config, text, agent=name, actions=actions):
            _permission_notified.mark(key)
            log.info("Sent permission-waiting notification for %s", name)


_denial_notified = RecentKeys(1800)
"""(agent, category, summary) refusals the humans were told about."""
_denial_log_offset: int | None = None
"""How far into the action log refusals have been read; None until the first check."""
_denial_log_inode: int | None = None
"""The log file read; the prune job's atomic rotation replaces it with a new one."""
_denial_watch_started = 0.0
"""When the watch began: refusals stamped earlier are never replayed."""
_denials_read: dict[tuple, None] = {}
"""Identities of refusals already read, oldest first, so re-reading a rotated log adds
nothing twice. Kept by count, not age: the log keeps its newest lines however old."""
_DENIALS_READ_LIMIT = 5000
"""More identities than the rotated log (2,000 lines) can hold."""
_denials_unsent: dict[tuple, dict] = {}
"""Notices no integration accepted, by (agent, category, summary); tried again next tick."""
_UNSENT_LIMIT = 50


def denial_text(name: str, record: dict, runtime: Runtime) -> str:
    """One refusal, for a person: what, why, and what they can actually do."""
    summary = str(record.get("summary") or record.get("tool") or "a tool call")[:120]
    category = str(record.get("category") or "")[:80]
    check = runtime.refusal_check or "An automatic safety check"
    why = f"{check} refused it" + (f" ({category})" if category else "")
    allow = f", then {runtime.refusal_allow}" if runtime.refusal_allow else ""
    route = (
        "No approval prompt was shown, and it cannot be approved from here. "
        f"If you want it done, allow it in the terminal (tmux attach -t {name}{allow}) "
        f"or do it yourself, and tell {name} to continue."
    )
    return (
        f"\U0001f6ab Refused — {name}\n"
        f"Action: {summary}\n{why}.\n{route}\n"
        "The backbone does not retry refused actions."
    )


def _new_denials(config: BackboneConfig) -> list[dict]:
    """Refusal records appended to the action log since the last check."""
    global _denial_log_offset, _denial_log_inode, _denial_watch_started
    path = config.action_log_path
    if _denial_log_offset is None:
        _denial_watch_started = time.time()  # nothing from before the watch is replayed
    try:
        status = path.stat()
    except OSError:
        _denial_log_offset, _denial_log_inode = 0, None
        return []
    size = status.st_size
    if _denial_log_offset is None:
        _denial_log_offset, _denial_log_inode = size, status.st_ino
        return []
    if status.st_ino != _denial_log_inode or size < _denial_log_offset:
        # Rotated (a new file) or truncated: re-read; identities skip what was read.
        _denial_log_offset, _denial_log_inode = 0, status.st_ino
    with path.open("rb") as log_file:
        log_file.seek(_denial_log_offset)
        chunk = log_file.read(size - _denial_log_offset)
    end = chunk.rfind(b"\n") + 1  # a line still being written waits for the next check
    _denial_log_offset += end
    records = []
    for line in chunk[:end].splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("action") != "permission_denied":
            continue
        try:
            stamp = float(record.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        # Byte order, not timestamps, decides what is new: concurrent hooks may
        # append out of timestamp order.
        identity = (record.get("session"), record.get("tool_use_id") or stamp)
        if stamp < _denial_watch_started or identity in _denials_read:
            continue
        _denials_read[identity] = None
        if len(_denials_read) > _DENIALS_READ_LIMIT:
            del _denials_read[next(iter(_denials_read))]
        records.append(record)
    return records


async def check_permission_denials(config: BackboneConfig) -> None:
    """Tell the humans about a refusal no dialog showed (Claude's auto-mode
    classifier, Codex's automatic reviewer): the agent looks busy, and nobody
    would know.

    One notice per agent and refused action within the dedup window, in the
    agent's own Telegram topic, without buttons: there is nothing to approve
    remotely. A notice no integration accepted is tried again next tick; a
    refusal from before the watch is never replayed, and no action is retried.
    """
    pending = [*_denials_unsent.values(), *_new_denials(config)]
    _denials_unsent.clear()
    tried = set()  # one send per action and pass, even when it fails
    for record in pending:
        name = str(record.get("session") or "")
        if name not in config.agents:
            continue
        key = (name, record.get("category"), record.get("summary"))
        if key in tried or _denial_notified.seen(
            key, ttl_seconds=config.timing.escalation_dedup_seconds
        ):
            continue
        tried.add(key)
        # The runtime that refused it, even if the agent's runtime changed since.
        spec = config.agents.get(name)
        runtime = get_runtime(str(record.get("runtime") or "") or (spec.runtime if spec else None))
        if await notify_humans(config, denial_text(name, record, runtime), agent=name):
            _denial_notified.mark(key)
            log.warning("Sent permission-denied notification for %s", name)
        elif key in _denials_unsent or len(_denials_unsent) < _UNSENT_LIMIT:
            _denials_unsent[key] = record  # the notice is retried, never the action


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
    """Known agents whose last recorded state was live but whose session is gone.

    An agent with a pending transition (``backbone agent restart``) is absent
    on purpose and is not reported."""
    offline: list[dict] = []
    try:
        known_states = await db.states.all()
        planned = set(await db.transitions.open_agents())
    except Exception:
        log.exception("Failed to read agent states for offline check")
        return offline

    for record in known_states:
        session_name = record["session_name"]
        if record.get("state", "unknown") == "unknown":
            continue
        if session_name in active_sessions or session_name not in config.agents:
            continue
        if session_name in planned:
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
                escalation_session,
                msg,
                config,
                db=db,
                priority=True,
                delivery_kind="escalation",
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
        key = (agent["session"], "offline")
        if expected_up and not _escalated.seen(
            key, ttl_seconds=config.timing.escalation_dedup_seconds
        ):
            accepted = False
            escalation_session = _escalation_session(config, agent["session"])
            if escalation_session and escalation_session in active_sessions:
                msg = format_unexpected_offline_notification(
                    agent["session"], agent["entity"], agent["pending_count"]
                )
                try:
                    report = await safe_deliver(
                        escalation_session,
                        msg,
                        config,
                        db=db,
                        priority=True,
                        delivery_kind="escalation",
                    )
                    accepted = report.outcome == DeliveryOutcome.DELIVERED or report.queued
                except Exception:
                    log.exception(
                        "Could not report offline agent %s to %s",
                        agent["entity"],
                        escalation_session,
                    )
            try:
                notified = await notify_humans(
                    config,
                    f"Agent {agent['entity']} went offline unexpectedly "
                    f"({agent['pending_count']} pending). It was not restarted.",
                    agent=agent["session"],
                )
                accepted = accepted or notified
            except Exception:
                log.exception("Could not report offline agent %s to humans", agent["entity"])
            if not accepted:
                # Keep the transition discoverable next tick. Dedup only
                # accepted alerts, including durable queue handoffs.
                continue
            _escalated.mark(key)
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


async def check_blocked(
    config: BackboneConfig, states: AgentStates, db: BackboneDB | None = None
) -> None:
    """Report provider/usage blocks to humans and the responsible agents, once per recipient."""
    for name, snapshot in states.items():
        if snapshot.state != AgentState.BLOCKED:
            continue
        reason = snapshot.reason or "its runtime"
        detail = f" ({snapshot.detail})" if snapshot.detail else ""
        what = "its usage limit" if reason == "quota" else reason
        message = (
            f"Agent {name} is blocked on {what}{detail}. Messages for it remain queued. "
            "Check the retry/reset time or reassign its work; the backbone does not restart it."
        )
        if _should_escalate(name, "blocked", config.timing.escalation_dedup_seconds):
            try:
                accepted = await notify_humans(config, message, agent=name)
            except Exception:
                accepted = False
                log.exception("Could not report blocked agent %s to humans", name)
            if not accepted:
                _escalated.forget((name, "blocked"))

        if db is None:
            continue
        recipients = {_escalation_session(config, name)}
        spec = config.agents.get(name)
        for tag in spec.tags if spec else ():
            if not tag.startswith("swarm:"):
                continue
            swarm = await db.swarms.get(tag.split(":", 1)[1])
            if swarm and swarm["status"] == "active":
                recipients.update((swarm["coordinator"], swarm["initiator"]))
        for recipient in sorted(r for r in recipients if r and r != name and r in config.agents):
            key = f"blocked:{recipient}"
            if not _should_escalate(name, key, config.timing.escalation_dedup_seconds):
                continue
            try:
                report = await safe_deliver(
                    recipient,
                    f"[via:backbone event:provider-blocked] {message}",
                    config,
                    db=db,
                    priority=True,
                    delivery_kind="escalation",
                )
                if report.outcome != DeliveryOutcome.DELIVERED and not report.queued:
                    _escalated.forget((name, key))
            except Exception:
                _escalated.forget((name, key))
                log.exception("Could not report blocked agent %s to %s", name, recipient)


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
                config, msg, agent=name, actions=plan_actions(config, name, prompt_id(snapshot))
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
                try:
                    report = await safe_deliver(
                        escalation_session,
                        orch_msg,
                        config,
                        db=db,
                        priority=True,
                        delivery_kind="escalation",
                    )
                except Exception:
                    log.exception(
                        "Could not notify %s about waiting plan for %s", escalation_session, name
                    )
                    continue
                # Only suppress another attempt when the message arrived or a
                # durable queue row exists. A failed write must retry next tick.
                if report.outcome == DeliveryOutcome.DELIVERED or report.queued:
                    _record_plan_notification(name, orch_ref)
                    log.info(
                        "Plan notification for %s -> %s (%s)",
                        name,
                        escalation_session,
                        report.outcome,
                    )
