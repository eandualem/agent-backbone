"""Per-agent mutation serialization, shared by store and lifecycle operations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps
from inspect import signature
from weakref import WeakValueDictionary


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task | None = None


_locks: WeakValueDictionary[tuple[int, str], _Entry] = WeakValueDictionary()


@asynccontextmanager
async def lifecycle_lock(name: str):
    """Serialize one agent; nested mutations by the same task reuse its lock."""
    key = (id(asyncio.get_running_loop()), name)
    entry = _locks.setdefault(key, _Entry())
    task = asyncio.current_task()
    if entry.owner is task:
        yield
        return
    async with entry.lock:
        entry.owner = task
        try:
            yield
        finally:
            entry.owner = None


def serialized_mutation(method):
    """Store methods take an agent name or AgentSpec as their first argument."""
    parameters = signature(method)
    agent_parameter = list(parameters.parameters)[1]

    @wraps(method)
    async def run(self, *args, **kwargs):
        agent = parameters.bind(self, *args, **kwargs).arguments[agent_parameter]
        name = agent if isinstance(agent, str) else agent.name
        async with lifecycle_lock(name):
            return await method(self, *args, **kwargs)

    return run
