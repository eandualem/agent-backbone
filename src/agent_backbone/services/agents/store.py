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

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_GITHUB_REMOTE_RE = re.compile(
    r"(?:github\.com[:/])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def sanitize_name(raw: str) -> str:
    """Turn a directory name into a valid tmux session / label value."""
    cleaned = _NAME_RE.sub("-", raw.strip()).strip("-.")
    return cleaned or "agent"


def parse_github_remote(url: str) -> str:
    """``owner/name`` from an https or ssh GitHub remote, else ``""``."""
    match = _GITHUB_REMOTE_RE.search(url.strip())
    return f"{match.group('owner')}/{match.group('repo')}" if match else ""


async def detect_repo(directory: Path) -> str:
    """The GitHub ``owner/name`` of a directory's ``origin`` remote, if any.

    Runs ``git`` as a subprocess without blocking the event loop (``discover``
    is called from request handlers).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(directory),
            "remote",
            "get-url",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return ""
    try:
        async with asyncio.timeout(5):
            stdout, _ = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    if proc.returncode != 0:
        return ""
    return parse_github_remote(stdout.decode())


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
            or (existing.runtime if existing else self.config.agents_section.default_runtime),
            model=model if model is not None else (existing.model if existing else None),
            # Keep the recorded repo only for a record that lived elsewhere (a
            # moved project); rediscovering the same checkout trusts what the
            # checkout says now, so a removed origin clears ownership.
            repo=await detect_repo(path)
            or (existing.repo if existing and existing.path != path else ""),
            watches=existing.watches if existing else (),
            tags=existing.tags if existing else (),
            env=dict(existing.env) if existing else {},
            description=existing.description if existing else "",
        )

    async def register(self, spec: AgentSpec) -> AgentSpec:
        """Insert or update an agent and publish the new snapshot."""
        await self._db.agents.upsert(
            spec.name,
            dir=spec.dir,
            runtime=spec.runtime,
            model=spec.model,
            repo=spec.repo,
            tags=list(spec.tags),
            env=dict(spec.env),
            description=spec.description,
        )
        for repo in spec.watches:
            await self._db.agents.add_watch(spec.name, repo)
        await self.refresh()
        return self._agents.get(spec.name) or spec

    async def update(self, name: str, **changes) -> AgentSpec:
        """Change fields on a known agent (dir, runtime, model, repo, tags, env, description)."""
        current = self._agents.get(name)
        if current is None:
            raise KeyError(name)
        allowed = {"dir", "runtime", "model", "repo", "tags", "env", "description"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        merged = {
            "dir": current.dir,
            "runtime": current.runtime,
            "model": current.model,
            "repo": current.repo,
            "tags": tuple(current.tags),
            "env": dict(current.env),
            "description": current.description,
        }
        merged.update(changes)
        if "tags" in changes:
            merged["tags"] = tuple(changes["tags"])
        spec = AgentSpec(name=name, watches=current.watches, **merged)
        return await self.register(spec)

    async def forget(self, name: str) -> bool:
        removed = await self._db.agents.delete(name)
        await self.refresh()
        return removed

    async def watch(self, name: str, repo: str) -> AgentSpec:
        if name not in self._agents:
            raise KeyError(name)
        await self._db.agents.add_watch(name, repo)
        await self.refresh()
        return self._agents.get(name)  # type: ignore[return-value]

    async def unwatch(self, name: str, repo: str) -> bool:
        removed = await self._db.agents.remove_watch(name, repo)
        await self.refresh()
        return removed

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
