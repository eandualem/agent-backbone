"""The agent store — the known agents, backed by the database.

Agents are discovered, not declared: the first ``agent start`` from a
directory records it. The store keeps an in-memory snapshot
(``AgentsConfig``) and publishes a fresh ``BackboneConfig`` to the app
whenever agents or settings change, so routing code always reads a
consistent frozen view.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.config import (
    AgentsConfig,
    AgentSpec,
    BackboneConfig,
    agents_from_rows,
    build_config,
    validate_setting,
)
from agent_backbone.git import detect_repo
from agent_backbone.services.agents._locks import lifecycle_lock, serialized_mutation
from agent_backbone.services.agents._validation import validate_agent_spec, validate_repo

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_name(raw: str) -> str:
    """Turn a directory name into a valid tmux session / label value."""
    cleaned = _NAME_RE.sub("-", raw.strip()).lstrip("-._").rstrip("-.")
    return cleaned or "agent"


_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def _flag(name: str, value: object) -> bool:
    """A boolean agent field from what the CLI or API handed over.

    The CLI's direct path (backbone down) passes the raw ``key=value`` text,
    and ``bool("False")`` is True — for ``unattended`` that would turn
    machine-wide trust *on* while the owner is switching it off.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


class AgentStore:
    """Known agents + settings snapshot, with change notification."""

    def __init__(
        self,
        db: BackboneDB,
        data_dir: Path,
        *,
        on_change: Callable[[BackboneConfig], Awaitable[None] | None] | None = None,
    ) -> None:
        self._db = db
        self._data_dir = data_dir
        self._on_change = on_change
        self._agents = AgentsConfig()
        self._settings: dict = {}
        self._config: BackboneConfig | None = None
        self._lock = asyncio.Lock()
        self._discovery_lock = asyncio.Lock()

    # --- LifecycleAware ---

    async def start(self) -> None:
        await self.refresh()

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "agents", "count": len(self._agents)}

    # --- Snapshots ---

    @property
    def agents(self) -> AgentsConfig:
        return self._agents

    @property
    def config(self) -> BackboneConfig:
        if self._config is None:
            self._config = build_config(self._data_dir, settings={}, agents=AgentsConfig())
        return self._config

    async def refresh(self) -> BackboneConfig:
        """Re-read settings and agents from the database and publish."""
        async with self._lock:
            self._settings = await self._db.settings.all()
            self._agents = agents_from_rows(await self._db.agents.list())
            self._config = build_config(
                self._data_dir, settings=self._settings, agents=self._agents
            )
        if self._on_change is not None:
            result = self._on_change(self._config)
            if asyncio.iscoroutine(result):
                await result
        return self._config

    # --- Discovery / registration ---

    async def discover(
        self,
        directory: str | Path,
        *,
        name: str | None = None,
        runtime: str | None = None,
        model: str | None = None,
    ) -> AgentSpec:
        """Describe an agent for a directory without saving it.

        When the name is taken by an agent registered elsewhere: if that
        directory is gone the project has moved and the record follows it;
        if it still exists this is a different project sharing a folder
        name, and the new one gets a numbered name (``app-2``).
        """
        path = Path(directory).expanduser().resolve()
        if name is None:
            matches = [agent for agent in self._agents if agent.path == path]
            if len(matches) > 1:
                raise ValueError("multiple agents use this directory; specify an agent name")
            if matches:
                name = matches[0].name
        agent_name = sanitize_name(name or path.name)
        existing = self._agents.get(agent_name)
        if existing is not None and existing.path != path and existing.path.is_dir():
            base = agent_name
            counter = 2
            while True:
                agent_name = f"{base}-{counter}"
                existing = self._agents.get(agent_name)
                if existing is None or existing.path == path or not existing.path.is_dir():
                    break
                counter += 1
        return AgentSpec(
            name=agent_name,
            dir=str(path),
            runtime=runtime
            or (existing.runtime if existing else self.config.launch.default_runtime),
            model=model
            if model is not None
            else (existing.model if existing and runtime in (None, existing.runtime) else None),
            # Keep the recorded repo only for a record that lived elsewhere (a
            # moved project); rediscovering the same checkout trusts what the
            # checkout says now, so a removed origin clears ownership.
            repo=await detect_repo(path)
            or (existing.repo if existing and existing.path != path else ""),
            watches=existing.watches if existing else (),
            tags=existing.tags if existing else (),
            env=dict(existing.env) if existing else {},
            description=existing.description if existing else "",
            always_on=existing.always_on if existing else False,
            # A freedom granted for one runtime does not follow the agent to
            # another: behind a different CLI it may mean a different thing.
            unattended=bool(
                existing and existing.unattended and runtime in (None, existing.runtime)
            ),
        )

    async def register_directory(
        self, directory: str | Path, *, watches: tuple[str, ...] = (), **options
    ) -> AgentSpec:
        """Reserve discovery's chosen name through registration, preserving live updates."""
        async with self._discovery_lock:
            while True:
                spec = await self.discover(directory, **options)
                async with lifecycle_lock(spec.name):
                    await self.refresh()
                    current = await self.discover(directory, **options)
                    if current.name != spec.name:
                        continue
                    return await self.register(current.with_watches(*watches))

    @serialized_mutation
    async def register(self, spec: AgentSpec) -> AgentSpec:
        """Insert or update an agent and publish the new snapshot."""
        validate_agent_spec(spec)
        await self._db.agents.upsert(
            spec.name,
            dir=spec.dir,
            runtime=spec.runtime,
            model=spec.model,
            repo=spec.repo,
            tags=list(spec.tags),
            env=dict(spec.env),
            description=spec.description,
            always_on=spec.always_on,
            unattended=spec.unattended,
        )
        for repo in spec.watches:
            await self._db.agents.add_watch(spec.name, repo)
        await self.refresh()
        return self._agents.get(spec.name) or spec

    @serialized_mutation
    async def update(self, name: str, **changes) -> AgentSpec:
        """Change fields on a known agent (dir, runtime, model, repo, tags, env,
        description, always_on, unattended)."""
        allowed = {
            "dir",
            "runtime",
            "model",
            "repo",
            "tags",
            "env",
            "description",
            "always_on",
            "unattended",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        for flag in ("always_on", "unattended"):
            if flag in changes:
                changes[flag] = _flag(flag, changes[flag])
        await self.refresh()
        current = self._agents.get(name)
        if current is None:
            raise KeyError(name)
        if changes.get("runtime", current.runtime) != current.runtime:
            changes.setdefault("unattended", False)
            changes.setdefault("model", None)
        validate_agent_spec(replace(current, **changes))
        if not await self._db.agents.update_fields(name, changes):
            raise KeyError(name)
        await self.refresh()
        current = self._agents.get(name)
        if current is None:
            raise KeyError(name)
        return current

    @serialized_mutation
    async def tag(self, name: str, tags: list[str], *, remove: bool = False) -> AgentSpec:
        if any(
            not tag
            or len(tag) > 100
            or tag.startswith(("swarm:", "role:", "task:"))
            or any(not c.isprintable() or c.isspace() for c in tag)
            for tag in tags
        ):
            raise ValueError(
                "tags must be printable without spaces; swarm:, role: and task: are reserved"
            )
        await self.refresh()
        spec = self._agents.get(name)
        if spec is None:
            raise KeyError(name)
        changed = (
            [tag for tag in spec.tags if tag not in tags]
            if remove
            else list(dict.fromkeys((*spec.tags, *tags)))
        )
        return await self.update(name, tags=changed)

    @serialized_mutation
    async def forget(self, name: str) -> bool:
        removed = await self._db.agents.delete(name)
        await self.refresh()
        return removed

    async def rename(self, name: str, new_name: str) -> AgentSpec:
        """Rename a stopped non-swarm agent and retain its runtime resume record."""
        from agent_backbone.fs import atomic_write_text
        from agent_backbone.services.terminal import session_exists

        if name == new_name:
            raise ValueError("the new name is the same as the current name")
        # Stable lock order prevents two cross-renames from deadlocking.
        async with lifecycle_lock(min(name, new_name)), lifecycle_lock(max(name, new_name)):
            await self.refresh()
            spec = self._agents.get(name)
            if spec is None:
                raise KeyError(name)
            validate_agent_spec(replace(spec, name=new_name))
            if (
                name == self.config.backbone.session_name
                or new_name == self.config.backbone.session_name
            ):
                raise ValueError("the backbone session name is reserved")
            if spec.swarm:
                raise ValueError("swarm member names belong to the swarm lifecycle")
            if await session_exists(name) or await session_exists(new_name):
                raise ValueError(
                    f"stop '{name}' before renaming; both session names must be unused"
                )
            source = self.config.state_dir / f"{name}.json"
            target = self.config.state_dir / f"{new_name}.json"
            if target.exists():
                raise ValueError(f"'{new_name}' already has saved state; choose another name")
            copied = False
            try:
                if source.exists():
                    atomic_write_text(target, source.read_text())
                    copied = True
                await self._db.agents.rename(name, new_name)
            except BaseException:
                if copied:
                    target.unlink(missing_ok=True)
                raise
            source.unlink(missing_ok=True)
            (self.config.state_dir / f"{name}.starting").unlink(missing_ok=True)
            await self.refresh()
            return self._agents.get(new_name)

    @serialized_mutation
    async def watch(self, name: str, repo: str) -> AgentSpec:
        validate_repo(repo)
        await self.refresh()
        if name not in self._agents:
            raise KeyError(name)
        await self._db.agents.add_watch(name, repo)
        await self.refresh()
        return self._agents.get(name)  # type: ignore[return-value]

    @serialized_mutation
    async def unwatch(self, name: str, repo: str) -> bool:
        removed = await self._db.agents.remove_watch(name, repo)
        await self.refresh()
        return removed

    @serialized_mutation
    async def touch_started(self, name: str) -> None:
        await self._db.agents.touch_started(name)

    # --- Settings ---

    async def set_setting(self, key: str, value) -> BackboneConfig:
        clean = validate_setting(key, value)
        await self._db.settings.set(key, clean)
        return await self.refresh()

    async def unset_setting(self, key: str) -> BackboneConfig:
        await self._db.settings.delete(key)
        return await self.refresh()
