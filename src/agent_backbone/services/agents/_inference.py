"""Agent state reconciliation: hook state first, terminal reading as fallback."""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.fs import atomic_write_text
from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents.models import (
    REASON_PERMISSION,
    REASON_PLAN,
    REASON_QUESTION,
    AgentState,
    StateSnapshot,
)
from agent_backbone.services.runtimes import (
    GENERIC_BUSY_FRAGMENTS,
    UNKNOWN,
    detect_runtime,
    get_runtime,
    sanitize_pane_content,
)
from agent_backbone.services.terminal import capture_pane

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

STARTING_TRUST_SECONDS = 120.0
"""How long a ``starting`` marker counts as fresh.

``start_agent`` writes it when the session is created; a hook overwrites it
within seconds and ``wait_until_ready`` clears it when the prompt shows. If
neither happened (a ``--no-wait`` start of a runtime without hooks) the
terminal decides after this window — a runtime takes seconds to start, not
the five minutes a hook state is trusted for.
"""


def _fresh_window(snapshot: StateSnapshot, stale_threshold: float) -> float:
    if snapshot.state == AgentState.STARTING:
        return min(stale_threshold, STARTING_TRUST_SECONDS)
    return stale_threshold


def _trust_stale_push(snapshot: StateSnapshot) -> bool:
    """Whether a stale hook snapshot is still worth using when the pane says nothing."""
    if snapshot.state in (AgentState.IDLE, AgentState.BUSY):
        return True
    if snapshot.state == AgentState.WAITING_FOR_HUMAN and snapshot.reason == REASON_PLAN:
        return bool(snapshot.plan_file and Path(snapshot.plan_file).exists())
    return False


def infer_state_from_pane(pane_content: str, runtime_hint: str | None = None) -> StateSnapshot:
    """Infer the agent state from visible terminal output (with evidence)."""
    return _with_diagnostics(
        _infer_state_from_pane(pane_content, runtime_hint), pane_content, runtime_hint
    )


def _with_diagnostics(
    snapshot: StateSnapshot, pane_content: str | None, runtime_hint: str | None
) -> StateSnapshot:
    """Attach classified observations without changing a state decision or reading a pane."""
    if not pane_content or not pane_content.strip():
        return snapshot
    runtime = get_runtime(runtime_hint)
    if runtime is UNKNOWN:
        runtime = detect_runtime(pane_content)
    return replace(
        snapshot, diagnostics=runtime.diagnostics(pane_content), diagnostics_observed=True
    )


def _infer_state_from_pane(pane_content: str, runtime_hint: str | None = None) -> StateSnapshot:
    lines = sanitize_pane_content(pane_content).strip().splitlines()
    if not lines:
        return StateSnapshot(state=AgentState.UNKNOWN, source="pull", evidence=["empty pane"])

    runtime = get_runtime(runtime_hint)
    if runtime is UNKNOWN:
        runtime = detect_runtime(pane_content)

    if detail := runtime.provider_failure(pane_content):
        return StateSnapshot(
            state=AgentState.BLOCKED,
            reason="provider",
            detail=detail,
            source="pull",
            evidence=[f"terminal shows a provider failure ({runtime.id}): {detail}"],
        )
    if runtime.detect_busy(pane_content):
        return StateSnapshot(
            state=AgentState.BUSY,
            source="pull",
            evidence=[f"terminal shows a busy marker ({runtime.id})"],
        )
    if runtime.detect_waiting_for_human(pane_content):
        return _dialog_snapshot(runtime, pane_content)
    if runtime.detect_idle(pane_content):
        return StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
            evidence=[f"terminal shows an empty prompt ({runtime.id})"],
        )

    recent = "\n".join(ln.strip().lower() for ln in lines[-20:] if ln.strip())
    if any(fragment in recent for fragment in GENERIC_BUSY_FRAGMENTS):
        return StateSnapshot(
            state=AgentState.BUSY, source="pull", evidence=["terminal shows thinking/tool output"]
        )

    return StateSnapshot(
        state=AgentState.UNKNOWN,
        source="pull",
        evidence=[f"terminal inconclusive: no prompt, busy or question marker ({runtime.id})"],
    )


def _dialog_snapshot(runtime, pane_content: str, prefix: list[str] | None = None) -> StateSnapshot:
    """``waiting_for_human`` as read from the terminal: a known permission
    prompt, or any dialog recognised by its numbered options."""
    known = runtime.prompt_markers and any(
        marker in sanitize_pane_content(pane_content).lower()[-2000:]
        for marker in runtime.prompt_markers
    )
    if runtime.detect_choice_dialog(pane_content):
        reason, seen = REASON_QUESTION, "a choice dialog (Enter would pick, not allow)"
    elif known:
        reason, seen = REASON_PERMISSION, "a permission prompt"
    else:
        reason, seen = REASON_QUESTION, "a dialog with numbered options"
    return StateSnapshot(
        state=AgentState.WAITING_FOR_HUMAN,
        reason=reason,
        source="pull",
        prompt_ref=dialog_ref(pane_content),
        evidence=[*(prefix or []), f"terminal shows {seen} ({runtime.id})"],
    )


def dialog_ref(pane_content: str) -> str:
    """A short digest of the dialog on screen.

    A terminal reading is stamped at the moment it is taken, so its
    timestamp cannot identify the prompt: it moves at every poll. What is on
    screen does not, until the agent moves on — so the tail of the pane is
    the identity of that prompt (see ``models.prompt_id``).
    """
    tail = [ln.strip() for ln in sanitize_pane_content(pane_content).splitlines() if ln.strip()]
    return hashlib.sha256("\n".join(tail[-10:]).encode()).hexdigest()[:12]


def note_submission(state_dir: Path, session: str) -> None:
    """Invalidate idle evidence older than a paste, without overwriting hooks."""
    atomic_write_text(state_dir / f"{session}.submitted", str(time.time()))


async def get_agent_state(
    state_dir: Path,
    session: str,
    stale_threshold: float = 300,
    *,
    runtime_hint: str | None = None,
    pane_content: str | None = None,
    since: float | None = None,
) -> StateSnapshot:
    snapshot = await _get_agent_state(
        state_dir,
        session,
        stale_threshold,
        runtime_hint=runtime_hint,
        pane_content=pane_content,
        since=since,
    )
    try:
        submitted = float((state_dir / f"{session}.submitted").read_text())
        if not math.isfinite(submitted) or (since is not None and submitted < since):
            return snapshot
    except (OSError, ValueError):
        return snapshot
    hook = read_state_file(state_dir, session)
    if hook and hook.timestamp > submitted:
        return snapshot
    if snapshot.state not in {AgentState.IDLE, AgentState.UNKNOWN}:
        return snapshot
    evidence = [*snapshot.evidence, "idle evidence predates the last submission"]
    if time.time() - submitted < 5:
        return replace(
            snapshot,
            state=AgentState.BUSY,
            source="delivery",
            evidence=[*evidence, "waiting for runtime input acknowledgement"],
        )
    # With no newer hook, re-read the terminal instead of trusting the idle hook
    # that preceded the send. This also works for runtimes without hooks.
    if pane_content is None:
        pane_content = await capture_pane(session)
    pull = infer_state_from_pane(pane_content or "", runtime_hint)
    return replace(
        snapshot,
        state=pull.state,
        reason=pull.reason,
        detail=pull.detail,
        source="pull",
        evidence=[*evidence, *pull.evidence],
    )


async def _get_agent_state(
    state_dir: Path,
    session: str,
    stale_threshold: float = 300.0,
    *,
    runtime_hint: str | None = None,
    pane_content: str | None = None,
    since: float | None = None,
) -> StateSnapshot:
    """Reconciled agent state.

    A fresh hook-written state (younger than ``stale_threshold``) is
    authoritative: modern CLIs keep their prompt visible while working, so
    the terminal alone cannot tell busy from idle. Stale or missing hook
    state is verified against the terminal. Every snapshot carries the
    evidence it was built from.
    """
    push = read_state_file(state_dir, session)
    # Readiness observes this launch only. Its bookkeeping marker must not
    # mask the actual prompt; ordinary state reads still honor the marker.
    if since is not None and push and (push.timestamp < since or push.state == AgentState.STARTING):
        push = None
    push_age = (time.time() - push.timestamp) if push else None
    if push:
        stale_threshold = _fresh_window(push, stale_threshold)

    if push and push_age is not None and push_age < stale_threshold:
        via = f", {push.event}" if push.event else ""
        push.evidence = [
            f"hook state '{push.state.value}' written {push_age:.0f}s ago (fresh{via})"
        ]
        if push.reason:
            push.evidence.append(f"reason: {push.reason}")
        if push.state == AgentState.IDLE:
            # The one thing a hook cannot see: a dialog drawn by the runtime
            # itself (Claude Code's resume picker arrives after SessionStart
            # already said idle). A dialog on screen beats the idle claim.
            if pane_content is None:
                pane_content = await capture_pane(session)
            runtime = get_runtime(runtime_hint)
            if runtime is UNKNOWN and pane_content:
                runtime = detect_runtime(pane_content)
            if pane_content and (detail := runtime.provider_failure(pane_content)):
                blocked = replace(
                    push,
                    state=AgentState.BLOCKED,
                    reason="provider",
                    detail=detail,
                    source="pull",
                    timestamp=time.time(),
                    evidence=[
                        *push.evidence,
                        f"terminal shows a provider failure ({runtime.id}): {detail}",
                    ],
                )
                return _with_diagnostics(blocked, pane_content, runtime_hint)
            if pane_content and runtime.detect_active_dialog(pane_content):
                dialog = _dialog_snapshot(runtime, pane_content, prefix=push.evidence)
                dialog.timestamp = time.time()
                dialog.current_issue = push.current_issue
                dialog.current_repo = push.current_repo
                dialog.session_id = push.session_id
                dialog.last_message = push.last_message
                dialog.runtime = push.runtime
                dialog.model = push.model
                dialog.evidence.append("the dialog on screen beats the hook's idle")
                return _with_diagnostics(dialog, pane_content, runtime_hint)
        if push.state == AgentState.WAITING_FOR_HUMAN and push.reason == REASON_PERMISSION:
            # The hook said "permission", but the runtime may since have drawn
            # a dialog of its own on top (Codex's rate-limit model switch),
            # where the affirmative key chooses: the screen wins.
            if pane_content is None:
                pane_content = await capture_pane(session)
            runtime = get_runtime(runtime_hint)
            if runtime is UNKNOWN and pane_content:
                runtime = detect_runtime(pane_content)
            if pane_content and runtime.detect_choice_dialog(pane_content):
                dialog = _dialog_snapshot(runtime, pane_content, prefix=push.evidence)
                dialog.timestamp = time.time()
                dialog.current_issue = push.current_issue
                dialog.current_repo = push.current_repo
                dialog.session_id = push.session_id
                dialog.last_message = push.last_message
                dialog.runtime = push.runtime
                dialog.model = push.model
                dialog.evidence.append("the choice dialog on screen beats the hook's permission")
                return _with_diagnostics(dialog, pane_content, runtime_hint)
        return _with_diagnostics(push, pane_content, runtime_hint)

    if pane_content is None:
        pane_content = await capture_pane(session)
    if pane_content:
        pull = infer_state_from_pane(pane_content, runtime_hint)
        pull.timestamp = time.time()
        if push:
            # State freshness and saved conversation metadata are independent.
            pull.session_id = push.session_id
            pull.last_message = push.last_message
            pull.runtime = push.runtime
            pull.model = push.model
            pull.evidence.insert(
                0, f"hook state '{push.state.value}' is stale ({push_age:.0f}s) — reading terminal"
            )
        else:
            pull.evidence.insert(0, "no hook state file — reading terminal")
        if pull.state != AgentState.UNKNOWN:
            if push and pull.state == AgentState.BLOCKED:
                pull.current_issue = push.current_issue
                pull.current_repo = push.current_repo
            return pull
        if push and _trust_stale_push(push):
            push.evidence = [
                *pull.evidence,
                f"terminal inconclusive; falling back to stale hook state '{push.state.value}'",
            ]
            return replace(
                push,
                diagnostics=pull.diagnostics,
                diagnostics_observed=pull.diagnostics_observed,
            )
        return pull

    if push and _trust_stale_push(push):
        push.evidence = [
            f"no terminal output; using stale hook state '{push.state.value}' ({push_age:.0f}s)"
        ]
        return _with_diagnostics(push, pane_content, runtime_hint)

    return _with_diagnostics(
        StateSnapshot(
            state=AgentState.UNKNOWN,
            source="default",
            session_id=push.session_id if push else None,
            last_message=push.last_message if push else None,
            runtime=push.runtime if push else None,
            model=push.model if push else None,
            evidence=[
                "stale hook state is not trustworthy and no terminal output"
                if push
                else "no hook state and no terminal output"
            ],
        ),
        pane_content,
        runtime_hint,
    )


async def agent_state(
    config: BackboneConfig, name: str, *, pane_content: str | None = None
) -> StateSnapshot:
    """``get_agent_state`` with the paths and thresholds taken from the configuration."""
    spec = config.agents.get(name)
    snapshot = await get_agent_state(
        config.state_dir,
        name,
        config.timing.stale_threshold_seconds,
        runtime_hint=spec.runtime if spec else None,
        pane_content=pane_content,
    )

    return bind_task(config, name, snapshot)


def bind_task(config: BackboneConfig, name: str, snapshot: StateSnapshot) -> StateSnapshot:
    """Explicit swarm task identity outranks issue mentions in tools/transcripts."""
    spec = config.agents.get(name)
    if spec and spec.swarm:
        for tag in spec.tags:
            if tag.startswith("task:") and "#" in tag:
                repo, number = tag[5:].rsplit("#", 1)
                if number.isdigit():
                    return replace(
                        snapshot,
                        current_issue=int(number),
                        current_repo=repo,
                        evidence=[*snapshot.evidence, "task bound by swarm registration"],
                    )
    return snapshot
