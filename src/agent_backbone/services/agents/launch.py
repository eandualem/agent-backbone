"""Starting, stopping and answering agent sessions — the one launch path for
the API, the CLI, Telegram and swarms."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.config import session_secret_keys
from agent_backbone.fs import atomic_write_text
from agent_backbone.git import git_write_paths
from agent_backbone.services.agents._file_reader import (
    clear_starting_marker,
    read_state_file,
    write_starting_marker,
)
from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.agents.instructions import instruction_preview
from agent_backbone.services.agents.models import AgentState
from agent_backbone.services.runtimes import (
    AGENT_ENV_KEY,
    RUNTIME_ENV_KEY,
    RUNTIMES,
    STATE_DIR_ENV_KEY,
    Runtime,
    get_runtime,
    read_brief,
    resolve_runtime,
    sanitize_pane_content,
)
from agent_backbone.services.terminal import (
    capture_pane,
    session_exists,
    start_session,
    stop_session,
)
from agent_backbone.skills import manifest_path, materialize, read_store, select_skills

if TYPE_CHECKING:
    from agent_backbone.config import AgentSpec, BackboneConfig
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)


def launch_environment(
    name: str,
    runtime: str,
    state_dir: Path | str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment exported into an agent session so shipped hooks can find the backbone.

    This is the whole contract: the runtime, the agent's name, the state
    directory, and whatever the agent itself is configured with. The
    backbone's secrets are not part of it and are stripped from the session
    (see ``session_secret_keys`` and ``start_session``'s ``scrub``).
    """
    env = {RUNTIME_ENV_KEY: runtime, AGENT_ENV_KEY: name}
    if state_dir:
        env[STATE_DIR_ENV_KEY] = str(state_dir)
    reserved = {RUNTIME_ENV_KEY, AGENT_ENV_KEY, STATE_DIR_ENV_KEY}
    for key, value in (extra or {}).items():
        if key in reserved:
            log.warning("Ignoring reserved variable %s in agent env for '%s'", key, name)
            continue
        env[key] = value
    return env


@dataclass(frozen=True)
class StartResult:
    """What ``start_agent`` did.

    ``ok`` means the session is up — started now, or ``already_running``.
    ``ready`` is ``wait_until_ready``'s outcome (``ready``, ``waiting_for_human``,
    ``timeout``, ``exited``) or ``not_waited``; ``evidence`` says why.
    """

    ok: bool
    already_running: bool = False
    ready: str = "not_waited"
    evidence: tuple[str, ...] = ()


def _age(timestamp: float, now: float) -> str:
    """``"3h"``-style age of a hook timestamp, for the start evidence."""
    seconds = max(0, int(now - timestamp))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


async def start_agent(
    spec: AgentSpec,
    config: BackboneConfig,
    *,
    runtime: str | None = None,
    model: str | None = None,
    resume: bool = False,
    brief_file: Path | str | None = None,
    db: BackboneDB | None = None,
    wait: bool = True,
    operation_id: str | None = None,
) -> StartResult:
    """Start an agent and retain a bounded record of the attempt and its outcome.

    A start is a fresh conversation unless ``resume`` is asked for: the
    owner decides when an agent continues, because a resumed agent trusts
    its own context over what happened in the checkout since (other
    runtimes, swarms, the shared memory). The saved runtime and model are
    reused either way.

    Diagnostic details contain classifications only. The human-facing result
    may include terminal evidence, paths or a session id; none of those are
    copied into the diagnostic history.
    """
    operation_id = operation_id or uuid.uuid4().hex
    started = time.monotonic()
    details: dict = {
        "stage": "preflight",
        "requested_runtime": runtime,
        "requested_model": model,
        "resume": resume,
        "resume_selection": "runtime_latest" if resume else "fresh",
        "model_source": "configured",
    }
    record = {
        "category": "startup",
        "operation_id": operation_id,
        "agent_name": spec.name,
        "source": "start",
        "runtime": runtime or spec.runtime,
        "model": model if model is not None else spec.model,
    }
    if db is not None:
        await db.diagnostics.record(
            **record, code="requested", severity="info", details=dict(details)
        )

    async def observe(pane: str) -> None:
        if db is None:
            return
        rt = get_runtime(runtime or spec.runtime)
        for signal in rt.diagnostics(pane):
            await db.diagnostics.record(
                category="runtime",
                code=signal.code,
                operation_id=operation_id,
                observation_key=signal.observation_key,
                severity=signal.severity,
                agent_name=spec.name,
                source="startup",
                runtime=rt.id,
                model=signal.model,
                details={
                    "stage": "readiness",
                    "reason": signal.reason,
                    "error_type": signal.error_type,
                    "http_status": signal.http_status,
                    "observed_model": signal.model,
                    "observed_effort": signal.observed_effort,
                    "requested_model": model if model is not None else spec.model,
                    "model_source": "terminal",
                },
            )

    try:
        result = await _start_agent(
            spec,
            config,
            runtime=runtime,
            model=model,
            resume=resume,
            brief_file=brief_file,
            db=db,
            wait=wait,
            details=details,
            observe=observe,
        )
    except Exception as exc:
        details.update(reason="exception", error_type=type(exc).__name__)
        log.error("Agent startup failed during %s (%s)", details["stage"], type(exc).__name__)
        details["duration_ms"] = round((time.monotonic() - started) * 1000)
        if db is not None:
            await db.diagnostics.record(**record, code="failed", severity="error", details=details)
        raise
    outcome = (
        "already_running" if result.already_running else result.ready if result.ok else "failed"
    )
    severity = (
        "error"
        if outcome in {"failed", "exited"}
        else "warning"
        if outcome == "timeout"
        else "info"
    )
    details["duration_ms"] = round((time.monotonic() - started) * 1000)
    if db is not None:
        await db.diagnostics.record(**record, code=outcome, severity=severity, details=details)
    log.info("Agent '%s' startup ended: %s", spec.name, outcome)
    return result


async def _start_agent(
    spec: AgentSpec,
    config: BackboneConfig,
    *,
    runtime: str | None,
    model: str | None,
    resume: bool,
    brief_file: Path | str | None,
    db: BackboneDB | None,
    wait: bool,
    details: dict,
    observe: Callable[[str], Awaitable[None]],
) -> StartResult:
    """Start an agent in its tmux session.

    The runtime's folder-trust dialog is answered ahead of launch
    (``agents.pre_trust``). The agent's brief — the common backbone brief
    (``agents.inject_brief``), or ``brief_file`` when the caller has its own,
    such as a swarm role brief — reaches the runtime the way its
    ``brief_mode`` says: at launch (Claude Code, Codex, Gemini, OpenCode) or
    queued as the first message the agent receives once it is at its prompt
    (Aider). A resumed session already has its brief; a plain shell has
    nobody to brief (pasting it would run it as commands).
    """
    if await session_exists(spec.name):
        log.info("Agent '%s' already running", spec.name)
        return StartResult(ok=True, already_running=True)

    if not spec.path.is_dir():
        details["reason"] = "directory_missing"
        log.error("Directory '%s' does not exist for agent '%s'", spec.path, spec.name)
        return StartResult(ok=False, evidence=(f"directory does not exist: {spec.path}",))

    runtime_id = runtime or spec.runtime
    if runtime_id not in RUNTIMES:
        details["reason"] = "unknown_runtime"
        log.error("Cannot start agent '%s': unknown runtime %s", spec.name, runtime_id)
        return StartResult(ok=False, evidence=(f"unknown runtime: {runtime_id}",))
    rt = RUNTIMES[runtime_id]
    effective_model = model if model is not None else spec.model
    section = config.launch
    details["stage"] = "preparation"
    if section.pre_trust:
        rt.pre_trust(spec.path)

    # The owner's explicit choice, or the swarm rule decided at this launch:
    # a member whose runtime confines it to the worktree never asks. Decided
    # here, not at registration, so flipping `swarm.unattended_members` or
    # switching a member to a runtime without a sandbox takes effect at its
    # next start instead of a persisted grant outliving the reason for it.
    unattended = spec.unattended or (
        spec.swarm is not None and config.swarm.unattended_members and rt.sandboxed
    )

    brief = Path(brief_file) if brief_file else None
    if rt.brief_mode != "none" and (brief is not None or section.inject_brief or spec.swarm):
        try:
            preview = instruction_preview(replace(spec, runtime=rt.id), config, brief_file=brief)
            brief = config.data_dir / "briefs" / f"{spec.name}.md"
            atomic_write_text(brief, preview["content"])
        except (OSError, ValueError) as exc:
            details.update(reason="brief_failed", error_type=type(exc).__name__)
            return StartResult(ok=False, evidence=(str(exc),))
    skill_evidence, skill_error = _materialize_skills(config, spec, rt)
    if skill_error is not None:
        details.update(reason="skills_failed")
        return StartResult(ok=False, evidence=(skill_error, *skill_evidence))
    resume_target: bool | str = resume
    resume_evidence: list[str] = skill_evidence
    last = read_state_file(config.state_dir, spec.name)
    # A session id from *another* runtime means nothing here. A record
    # without a runtime (an older state file, or a hook wired by hand
    # outside a backbone session) is this agent's own.
    own_session = (
        last.session_id
        if last is not None and last.session_id and last.runtime in (None, rt.id)
        else None
    )
    saved_age = _age(last.timestamp, time.time()) if last is not None else ""
    if resume and last is not None and last.session_id and own_session is None:
        resume_evidence.append(
            f"last session id belongs to {last.runtime}; using {rt.id}'s own resume"
        )
    elif resume and own_session and rt.supports_exact_resume:
        resume_target = own_session
        details["resume_selection"] = "known_session"
        resume_evidence.append(
            f"resuming the session the backbone last saw: {own_session} ({saved_age} old)"
        )
    elif not resume and own_session and rt.supports_exact_resume:
        resume_evidence.append(
            f"fresh conversation; the previous one ({saved_age} old) is still available: "
            f"backbone agent resume {spec.name}"
        )
    details["stage"] = "command"
    try:
        command = rt.build_command(
            model=effective_model,
            resume=resume_target,
            brief_file=brief,
            pre_trust=section.pre_trust,
            data_dir=config.data_dir,
            state_dir=config.state_dir,
            unattended=unattended,
            writable_dirs=_writable_dirs(spec.path, section.writable_dirs),
            auto_review=section.auto_review,
        )
    except RuntimeError as exc:
        details.update(reason="command_failed", error_type=type(exc).__name__)
        log.error("Cannot start agent '%s': %s", spec.name, exc)
        return StartResult(ok=False, evidence=(str(exc),))

    details["stage"] = "preparation"
    if unattended:
        rt.prepare_unattended()

    extra_env = {**spec.env, **rt.launch_env(rt.split_model(effective_model)[0])}
    environment = launch_environment(
        spec.name,
        rt.id,
        config.state_dir,
        {
            **extra_env,
            **rt.hook_launch_env(config.data_dir, config.state_dir, env=extra_env),
        },
    )
    # `starting` lives in its own marker file, written before the launch: a
    # hook write newer than the marker outranks it, ``wait_until_ready``
    # clears it when the prompt shows, and ``get_agent_state`` stops
    # trusting it after a short window regardless.
    details["stage"] = "launch"
    launched_at = time.time()
    write_starting_marker(config.state_dir, spec.name, launched_at)
    ok = await start_session(
        spec.name,
        working_dir=str(spec.path),
        command=command,
        environment=environment,
        scrub=session_secret_keys(config.data_dir),
        mouse=rt.mouse_scroll,
    )
    if not ok:
        details["reason"] = "session_failed"
        clear_starting_marker(config.state_dir, spec.name)
        return StartResult(ok=False, evidence=("tmux could not create the session",))
    extra = f", model: {effective_model}" if effective_model else ""
    if unattended:
        extra += ", unattended"
    log.info("Agent '%s' started (runtime: %s%s)", spec.name, rt.id, extra)

    ready, evidence = "not_waited", []
    if wait:
        details["stage"] = "readiness"
        ready, evidence = await wait_until_ready(
            spec.name,
            state_dir=config.state_dir,
            runtime=rt,
            timeout=config.timing.start_timeout_seconds,
            stale_threshold=config.timing.stale_threshold_seconds,
            since=launched_at,
            details=details,
            observe=observe,
        )

    # No launch-time injection for this runtime: the brief is queued as the
    # first message, delivered by the monitor once the agent is at its prompt.
    if rt.brief_mode == "message" and brief is not None and not resume and ready != "exited":
        await _queue_brief(db, spec.name, brief)
    return StartResult(ok=True, ready=ready, evidence=tuple(resume_evidence + evidence))


def _materialize_skills(
    config: BackboneConfig, spec: AgentSpec, rt: Runtime
) -> tuple[list[str], str | None]:
    """Link the skills tagged for this agent into the directories its runtime reads.

    Evidence lines for the start result, and an error when a selected skill
    does not resolve (the store's referential integrity is checked at every
    launch, like a missing policy). A name the repository already owns is
    skipped, not fatal: the repository's own skill wins.
    """
    store = config.skills.store_path
    if store is None or not rt.skill_dirs:
        return [], None
    try:
        selected = select_skills(read_store(store), spec.tags, spec.name)
        outcome = materialize(
            store, spec.path, rt.skill_dirs, selected, manifest_path(config.data_dir, spec.name)
        )
    except OSError as exc:
        return [], f"skills: could not link into {spec.path}: {exc}"
    evidence: list[str] = []
    if outcome.linked:
        names = ", ".join(skill.name for skill in selected)
        evidence.append(f"skills: {names} linked in {', '.join(rt.skill_dirs)} from {store}")
    evidence.extend(f"skills: {line}" for line in outcome.conflicts)
    if outcome.broken:
        return evidence, "skills: " + "; ".join(outcome.broken)
    return evidence, None


def _writable_dirs(agent_dir: Path, configured: tuple[str, ...]) -> tuple[str, ...]:
    """Explicit tooling roots and validated Git commit paths, without hooks/config."""
    return tuple(dict.fromkeys((*configured, *git_write_paths(agent_dir))))


async def _queue_brief(db: BackboneDB | None, name: str, brief: Path) -> None:
    text = read_brief(brief)
    if db is None or text is None:
        if text is not None:
            log.info("No database handle: agent '%s' starts without its brief", name)
        return
    try:
        await db.queue.enqueue(
            session_name=name,
            message=f"[via:backbone] {text}",
            delivery_kind="direct_message",
            source="agent-brief",
        )
    except Exception:
        log.exception("Could not queue the brief for '%s' (non-fatal)", name)


async def wait_until_ready(
    name: str,
    *,
    state_dir: Path | str,
    runtime: Runtime | str,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
    stale_threshold: float = 300.0,
    since: float | None = None,
    details: dict | None = None,
    observe: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[str]]:
    """Wait until the agent is at its prompt.

    Returns ``(outcome, evidence)`` with outcome ``ready``, ``waiting_for_human``
    (the runtime is asking something, e.g. a folder-trust prompt), ``exited``
    or ``timeout``. Readiness is a hook-written ``idle`` state newer than
    ``since`` (the launch; default: now), or — for runtimes without hooks — a
    visible empty prompt in the terminal, which also clears the ``starting``
    marker ``start_agent`` wrote (the hook state file itself is never touched).
    """
    started = time.monotonic()
    wall_started = since if since is not None else time.time()
    rt = get_runtime(runtime)
    state_path = Path(state_dir).expanduser()
    last_pane = ""
    details = details if details is not None else {}
    while True:
        if not await session_exists(name):
            details.update(state="offline", state_source="terminal", reason="session_exited")
            clear_starting_marker(state_path, name)
            return "exited", ["tmux session ended before the agent reached its prompt"]

        pane = await capture_pane(name, lines=60)
        if pane:
            last_pane = pane
            if observe is not None:
                await observe(pane)
        snapshot = await get_agent_state(
            state_path,
            name,
            stale_threshold,
            runtime_hint=rt.id,
            pane_content=pane,
            since=wall_started,
        )
        details.update(
            state=snapshot.state.value,
            state_source=snapshot.source,
            reason=snapshot.reason
            if snapshot.reason in {"plan", "permission", "question", "quota", "provider"}
            else None,
        )
        if snapshot.state in {AgentState.IDLE, AgentState.WAITING_FOR_HUMAN}:
            clear_starting_marker(state_path, name)
            if snapshot.state == AgentState.IDLE:
                return "ready", snapshot.evidence
            return "waiting_for_human", [*snapshot.evidence, *_pane_tail(pane)]

        if time.monotonic() - started >= timeout:
            details["reason"] = details.get("reason") or "readiness_timeout"
            tail = [ln for ln in last_pane.strip().splitlines() if ln.strip()][-3:]
            return "timeout", [f"no prompt after {timeout:.0f}s; last lines: {tail}"]
        await asyncio.sleep(poll_interval)


def _pane_tail(pane: str, lines: int = 6) -> list[str]:
    return [ln for ln in sanitize_pane_content(pane).splitlines() if ln.strip()][-lines:]


async def approve_agent(
    name: str, *, runtime: str | None = None, settle_seconds: float = 1.0
) -> tuple[str, list[str]]:
    """Answer the permission prompt an agent's runtime is showing right now.

    Returns ``(outcome, evidence)``: ``approved`` (keys sent; evidence says
    whether the dialog cleared), ``not_waiting`` (the terminal shows no
    permission prompt — nothing is sent, so a stale hook state or an idle
    prompt with typed text can never be "approved"), ``not_permission`` (a
    choice dialog such as a model switch — ``Enter`` would pick, not allow),
    ``unsupported`` (no verified answer sequence for this runtime),
    ``offline`` or ``failed``.
    The backbone answers only what is on screen: it never guesses.

    The gate is ``detect_active_dialog`` (the dialog must be the runtime's
    most recent surface — no input prompt or placeholder below it), not the
    looser ``detect_waiting_for_human`` state reading, so a stale
    "press enter to confirm" line above an idle prompt is ``not_waiting``.
    What remains is the window between the capture and the keystroke: tmux
    has no check-and-send, so a dialog that the human answers in exactly
    that instant would receive an extra ``Enter`` at an empty prompt. Only
    runtime IPC (hook adapters, #88) closes that; the after-capture reports
    what actually happened.
    """
    if not await session_exists(name):
        return "offline", [f"no tmux session named '{name}'"]
    pane = await capture_pane(name, lines=60)
    rt = await resolve_runtime(name, hint=runtime, pane_content=pane)
    if not rt.approve_keys:
        return "unsupported", [f"no verified way to answer a {rt.id} permission prompt"]
    tail = [ln.strip() for ln in sanitize_pane_content(pane).splitlines() if ln.strip()][-8:]
    if not rt.detect_active_dialog(pane):
        return "not_waiting", ["terminal shows no active permission prompt:", *tail]
    if rt.detect_choice_dialog(pane):
        return "not_permission", [
            "the dialog on screen is a choice, not a permission prompt — Enter would pick "
            f"its first option; answer it in the terminal: tmux attach -t {name}",
            *tail,
        ]
    if not await rt.approve_prompt(name):
        return "failed", ["tmux refused the keys", *tail]
    await asyncio.sleep(settle_seconds)
    after = await capture_pane(name, lines=60)
    cleared = not rt.detect_active_dialog(after)
    verdict = "prompt cleared" if cleared else "prompt still visible after answering"
    log.info("Approved a %s permission prompt on '%s' (%s)", rt.id, name, verdict)
    return "approved", [f"answered with {' '.join(rt.approve_keys)}; {verdict}", *tail]


async def deny_agent(
    name: str, *, runtime: str | None = None, settle_seconds: float = 1.0
) -> tuple[str, list[str]]:
    """Refuse the permission prompt an agent's runtime is showing right now.

    The mirror of ``approve_agent`` with the runtime's refusing key
    (``deny_keys``): ``denied``, ``not_waiting``, ``unsupported``, ``offline``
    or ``failed``, with the same gate — only a dialog on screen is answered.
    """
    if not await session_exists(name):
        return "offline", [f"no tmux session named '{name}'"]
    pane = await capture_pane(name, lines=60)
    rt = await resolve_runtime(name, hint=runtime, pane_content=pane)
    if not rt.deny_keys:
        return "unsupported", [f"no verified way to refuse a {rt.id} permission prompt"]
    tail = [ln.strip() for ln in sanitize_pane_content(pane).splitlines() if ln.strip()][-8:]
    if not rt.detect_active_dialog(pane):
        return "not_waiting", ["terminal shows no active permission prompt:", *tail]
    if not await rt.deny_prompt(name):
        return "failed", ["tmux refused the keystroke"]
    await asyncio.sleep(settle_seconds)
    after = await capture_pane(name, lines=60)
    cleared = "cleared" if not rt.detect_active_dialog(after) else "still visible"
    return "denied", [f"sent {' '.join(rt.deny_keys)} to {rt.id}; dialog {cleared}", *tail]


async def plan_control(
    name: str, action: str, *, runtime: str | None = None
) -> tuple[str, list[str]]:
    """Approve or reject the plan an agent is presenting, through its runtime.

    ``action`` is ``approve`` or ``reject``. Returns ``(outcome, evidence)``:
    ``approved`` / ``rejected`` (the runtime's keys were sent), ``unsupported``
    (the runtime has no plan mode the backbone can drive — nothing is typed,
    so Claude Code's key sequence can never reach a Codex or OpenCode
    terminal), ``offline`` or ``failed``. Rejecting only leaves plan mode;
    the feedback itself is a ``plan_response`` delivery through
    ``safe_deliver``. Callers gate on ``security.allow_remote_plan_control``
    and on the agent actually waiting for a plan decision.
    """
    if action not in ("approve", "reject"):
        raise ValueError(f"unknown plan action {action!r}")
    if not await session_exists(name):
        return "offline", [f"no tmux session named '{name}'"]
    pane = await capture_pane(name, lines=60)
    rt = await resolve_runtime(name, hint=runtime, pane_content=pane)
    if not rt.supports_plan_control:
        return "unsupported", [
            f"{rt.display_name} has no plan mode the backbone can drive; nothing was sent"
        ]
    keys = rt.plan_approve_keys if action == "approve" else rt.plan_reject_keys
    sent = await (rt.approve_plan(name) if action == "approve" else rt.reject_plan(name))
    if sent == 0:
        return "failed", ["tmux refused the keys; nothing was sent"]
    if sent < len(keys):
        # Part of the sequence went in (Claude: Escape without "[Z" leaves
        # plan mode without accepting) — say exactly what happened.
        return "failed", [
            f"sent {' '.join(keys[:sent])} but tmux refused {keys[sent]}; "
            f"the {rt.id} session may have left plan mode — check it before retrying"
        ]
    outcome = "approved" if action == "approve" else "rejected"
    log.info("Plan %s on '%s' via %s", outcome, name, rt.id)
    return outcome, [f"sent {' '.join(keys)} to {rt.id}"]


async def stop_agent(name: str) -> bool:
    """Stop a single agent session."""
    ok = await stop_session(name)
    if ok:
        log.info("Agent '%s' stopped", name)
    return ok
