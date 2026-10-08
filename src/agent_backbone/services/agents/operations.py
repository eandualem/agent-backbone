"""Agent operations shared by the API routes and the CLI's direct mode.

The CLI talks to the running API when it is up and to the database directly
when it is not; both paths call these functions so the two never drift.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_backbone.services.agents import launch
from agent_backbone.services.agents._locks import lifecycle_lock
from agent_backbone.services.agents._validation import validate_agent_spec
from agent_backbone.services.agents.launch import StartResult
from agent_backbone.services.agents.store import check_agent_tags, refuse_case_twin
from agent_backbone.services.runtimes import (
    RUNTIMES,
    install_hint,
    resolve_runtime,
    unavailable,
)
from agent_backbone.services.terminal import session_exists
from agent_backbone.skills import manifest_links, manifest_path

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agent_backbone.config import AgentSpec, BackboneConfig
    from agent_backbone.services.agents.store import AgentStore
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartRequest:
    """What ``agent start`` was asked to do.

    ``directory`` discovers (or re-registers) the agent for that directory;
    the name defaults to the directory name. Without it the agent must
    already be known by ``name``. ``inbox_only`` registers the agent as
    inbox-only instead of launching it.
    """

    name: str | None = None
    directory: str | None = None
    runtime: str | None = None
    model: str | None = None
    resume: bool = False
    session_id: str | None = None
    """Resume exactly this conversation (a fleet snapshot's); never another."""
    watch: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    """Added to the agent's tags before it launches, so its first session
    already has the skills and policies they select."""
    wait: bool = True
    inbox_only: bool = False
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


async def resolve_agent(store: AgentStore, req: StartRequest) -> AgentSpec:
    """The agent a start request refers to, registered and up to date.

    A directory is discovered and registered (with any extra watches); a
    bare name is looked up. A runtime or model given at start becomes the
    agent's recorded setting, so the next bare ``agent start NAME`` reuses
    it. Raises KeyError for an unknown name and ValueError when neither a
    name nor a directory was given.
    """
    started = time.monotonic()
    try:
        return await _resolve_agent(store, req)
    except (KeyError, ValueError) as exc:
        await _record_start_failure(store._db, req, "resolve", exc, started)
        raise


async def _record_start_failure(
    db: BackboneDB | None,
    req: StartRequest,
    stage: str,
    error: Exception,
    started: float,
    spec: AgentSpec | None = None,
) -> None:
    if db is None:
        return
    reason = "validation_failed" if stage == "preflight" else "invalid_request"
    await db.diagnostics.record(
        category="startup",
        code="failed",
        operation_id=req.operation_id,
        severity="error",
        agent_name=spec.name if spec else req.name or "",
        source="start",
        runtime=req.runtime or (spec.runtime if spec else ""),
        model=req.model if req.model is not None else spec.model if spec else None,
        details={
            "stage": stage,
            "reason": "unknown_agent" if isinstance(error, KeyError) else reason,
            "error_type": type(error).__name__,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "requested_runtime": req.runtime,
            "requested_model": req.model,
            "resume": req.resume,
            "model_source": "configured",
        },
    )


async def _resolve_agent(store: AgentStore, req: StartRequest) -> AgentSpec:
    refuse_case_twin(store.agents, req.name)
    if req.inbox_only and req.watch:
        raise ValueError("an inbox-only agent takes no part in GitHub routing: drop the watches")
    if req.inbox_only and req.resume:
        raise ValueError("an inbox-only agent is never launched: there is nothing to resume")
    check_agent_tags(req.tags)
    if req.directory:
        spec = await store.register_directory(
            req.directory,
            name=req.name,
            runtime=req.runtime,
            model=req.model,
            watches=req.watch,
            inbox_only=req.inbox_only,
        )
        return await store.tag(spec.name, list(req.tags)) if req.tags else spec

    if not req.name:
        raise ValueError("name or dir is required")
    spec = store.agents.get(req.name)
    if spec is None:
        raise KeyError(req.name)
    for repo in req.watch:
        spec = await store.watch(req.name, repo)
    changes: dict = {}
    if req.runtime and req.runtime != spec.runtime:
        changes["runtime"] = req.runtime
    if req.model is not None and (req.model != spec.model or "runtime" in changes):
        changes["model"] = req.model
    if req.inbox_only and not spec.inbox_only:
        changes["inbox_only"] = True
    if changes:
        spec = await store.update(spec.name, **changes)
    if req.tags:
        spec = await store.tag(spec.name, list(req.tags))
    return spec


async def relink_skills(
    config: BackboneConfig, specs: Iterable[AgentSpec], *, skill: str | None = None
) -> list[str]:
    """Bring the skill links of the running agents among ``specs`` in line
    with the store and their tags now, as their next launch would.

    A skill added or retagged (``skill``, whose content may have changed),
    or an agent tagged, then reaches a running session without a restart:
    Claude Code and Codex load a skill linked mid-session, and a CLI that
    reads skills only at start is asked to load them again
    (``Runtime.reload_skills``). Evidence lines, each naming its agent.
    """
    evidence: list[str] = []
    for spec in specs:
        try:
            if spec.inbox_only or not await session_exists(spec.name):
                continue
            async with lifecycle_lock(spec.name):
                # The runtime the session runs, which ``agent set`` may have changed since.
                runtime = await resolve_runtime(spec.name)
                manifest = manifest_path(config.data_dir, spec.name)
                before = manifest_links(manifest)
                lines, error = launch._materialize_skills(config, spec, runtime)
                after = manifest_links(manifest)
                touched = skill is not None and any(
                    rel.rpartition("/")[2] == skill for rel in before | after
                )
                if before != after or touched:
                    if note := runtime.reload_skills(config.state_dir, spec.name):
                        lines = [*lines, f"skills: {note}"]
                    elif any(cap.id == "skills-live" for cap in unavailable(runtime.id)):
                        lines = [
                            *lines,
                            f"skills: not verified that {runtime.display_name} loads a "
                            "change mid-session; restart it to be sure",
                        ]
        except Exception as exc:  # the change itself is saved; its next launch links it
            log.warning("Could not relink the skills of '%s': %s", spec.name, exc)
            lines, error = [], f"skills: could not relink now ({type(exc).__name__})"
        evidence.extend(f"{spec.name}: {line}" for line in (*lines, *filter(None, [error])))
    return evidence


async def stop_agent_session(config: BackboneConfig, name: str) -> bool:
    """Stop an agent's tmux session. The backbone's own session is refused
    (``ValueError``) on every surface — API, CLI, Telegram — from here."""
    if name == config.backbone.session_name:
        raise ValueError("refusing to stop the backbone's own session")
    spec = config.agents.get(name)
    if spec is not None and spec.inbox_only:
        # A session with its name is not the agent's: never stop it on its behalf.
        raise ValueError(f"'{name}' is inbox-only: it has no session to stop")
    async with lifecycle_lock(name):
        return await launch.stop_agent(name)


async def forget_agent(store: AgentStore, name: str) -> bool:
    """Remove an agent from the backbone. A running session is refused
    (``RuntimeError``): stop it first. Returns False for an unknown name.

    Holds the agent's lifecycle lock, so a start in progress finishes first
    and the session check right before the delete sees it."""
    async with lifecycle_lock(name):
        if await session_exists(name):
            raise RuntimeError(f"'{name}' is running — stop it first")
        return await store.forget(name)


async def start_resolved(
    store: AgentStore,
    config: BackboneConfig,
    spec: AgentSpec,
    req: StartRequest,
    *,
    db: BackboneDB | None,
) -> StartResult:
    """Start a resolved agent. Raises ValueError for a runtime or directory that cannot work.

    An inbox-only agent is never launched: asked for as such it is only
    registered (``ready`` is ``inbox_only``); any other start is refused."""
    started = time.monotonic()
    runtime = req.runtime or spec.runtime
    try:
        if spec.inbox_only and not req.inbox_only:
            raise ValueError(
                f"'{spec.name}' is inbox-only: it is never launched "
                f"(backbone agent set {spec.name} inbox_only=false to launch it)"
            )
        validate_agent_spec(spec)
        # Nothing is launched for an inbox-only agent: runtime and directory do not matter.
        if not spec.inbox_only:
            if runtime not in RUNTIMES:
                raise ValueError(f"Unknown runtime: {runtime}")
            if not RUNTIMES[runtime].available():
                raise ValueError(f"Runtime '{runtime}' binary not found: {install_hint()}")
            if not spec.path.is_dir():
                raise ValueError(f"Directory does not exist: {spec.path}")
    except ValueError as exc:
        await _record_start_failure(db, req, "preflight", exc, started, spec)
        raise
    async with lifecycle_lock(spec.name):
        try:
            await store.refresh()
            current = store.agents.get(spec.name)
            if current is None:
                raise ValueError(f"Agent '{spec.name}' was forgotten before startup")
            if current != spec:
                raise ValueError(f"Agent '{spec.name}' changed before startup; retry the start")
        except ValueError as exc:
            await _record_start_failure(db, req, "preflight", exc, started, spec)
            raise
        if spec.inbox_only:
            return StartResult(
                ok=True,
                ready="inbox_only",
                evidence=(
                    f"never launched: it reads messages with backbone inbox --agent {spec.name}",
                ),
            )
        result = await launch.start_agent(
            spec,
            config,
            runtime=runtime,
            model=req.model if req.model is not None else spec.model,
            resume=req.resume,
            session_id=req.session_id,
            db=db,
            wait=req.wait,
            operation_id=req.operation_id,
        )
        if result.ok and not result.already_running:
            await store.touch_started(spec.name)
    return result
