"""Agent operations shared by the API routes and the CLI's direct mode.

The CLI talks to the running API when it is up and to the database directly
when it is not; both paths call these functions so the two never drift.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_backbone.services.agents import launch
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
    resume: bool = False
    watch: tuple[str, ...] = ()
    wait: bool = True


async def resolve_agent(store: AgentStore, req: StartRequest) -> AgentSpec:
    """The agent a start request refers to, registered and up to date.

    A directory is discovered and registered (with any extra watches); a
    bare name is looked up. A runtime or model given at start becomes the
    agent's recorded setting, so the next bare ``agent start NAME`` reuses
    it. Raises KeyError for an unknown name and ValueError when neither a
    name nor a directory was given.
    """
    if req.directory:
        spec = await store.discover(
            req.directory, name=req.name, runtime=req.runtime, model=req.model
        )
        if req.watch:
            spec = spec.with_watches(*req.watch)
        return await store.register(spec)

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
    if req.model is not None and req.model != spec.model:
        changes["model"] = req.model
    if changes:
        spec = await store.update(spec.name, **changes)
    return spec


_lifecycle_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
"""One lock per agent name: a start and a forget never interleave."""


def lifecycle_lock(name: str) -> asyncio.Lock:
    return _lifecycle_locks[name]


async def stop_agent_session(config: BackboneConfig, name: str) -> bool:
    """Stop an agent's tmux session. The backbone's own session is refused
    (``ValueError``) on every surface — API, CLI, Telegram — from here."""
    if name == config.backbone.session_name:
        raise ValueError("refusing to stop the backbone's own session")
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
    runtime = req.runtime or spec.runtime
    if runtime not in RUNTIMES:
        raise ValueError(f"Unknown runtime: {runtime}")
    if not RUNTIMES[runtime].available():
        raise ValueError(f"Runtime '{runtime}' binary not found")
    if not spec.path.is_dir():
        raise ValueError(f"Directory does not exist: {spec.path}")
    async with lifecycle_lock(spec.name):
        result = await launch.start_agent(
            spec,
            config,
            runtime=runtime,
            model=req.model if req.model is not None else spec.model,
            resume=req.resume,
            db=db,
            wait=req.wait,
        )
        if result.ok and not result.already_running:
            await store.touch_started(spec.name)
    return result
