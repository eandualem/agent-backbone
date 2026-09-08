"""Agent operations shared by the API routes and the CLI's direct mode.

The CLI talks to the running API when it is up and to the database directly
when it is not; both paths call these functions so the two never drift.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_backbone.services.agents import launch
from agent_backbone.services.agents._locks import lifecycle_lock
from agent_backbone.services.agents._validation import validate_agent_spec
from agent_backbone.services.agents.launch import StartResult
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.terminal import session_exists

if TYPE_CHECKING:
    from agent_backbone.config import AgentSpec, BackboneConfig
    from agent_backbone.services.agents.store import AgentStore
    from agent_backbone.services.database import BackboneDB


@dataclass(frozen=True)
class StartRequest:
    """What ``agent start`` was asked to do.

    ``directory`` discovers (or re-registers) the agent for that directory;
    the name defaults to the directory name. Without it the agent must
    already be known by ``name``.
    """

    name: str | None = None
    directory: str | None = None
    runtime: str | None = None
    model: str | None = None
    resume: bool | None = None
    watch: tuple[str, ...] = ()
    wait: bool = True
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
    if req.directory:
        return await store.register_directory(
            req.directory,
            name=req.name,
            runtime=req.runtime,
            model=req.model,
            watches=req.watch,
        )

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
    if changes:
        spec = await store.update(spec.name, **changes)
    return spec


async def stop_agent_session(config: BackboneConfig, name: str) -> bool:
    """Stop an agent's tmux session. The backbone's own session is refused
    (``ValueError``) on every surface — API, CLI, Telegram — from here."""
    if name == config.backbone.session_name:
        raise ValueError("refusing to stop the backbone's own session")
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
    """Start a resolved agent. Raises ValueError for a runtime or directory that cannot work."""
    started = time.monotonic()
    runtime = req.runtime or spec.runtime
    try:
        validate_agent_spec(spec)
        if runtime not in RUNTIMES:
            raise ValueError(f"Unknown runtime: {runtime}")
        if not RUNTIMES[runtime].available():
            raise ValueError(f"Runtime '{runtime}' binary not found")
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
        resume = launch.resolve_resume(config, spec.name, runtime, req.resume)
        result = await launch.start_agent(
            spec,
            config,
            runtime=runtime,
            model=req.model if req.model is not None else spec.model,
            resume=resume,
            db=db,
            wait=req.wait,
            operation_id=req.operation_id,
        )
        if result.ok and not result.already_running:
            await store.touch_started(spec.name)
    return result
