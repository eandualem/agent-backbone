"""Memory samples under pressure: tie a slowdown to an agent, a swarm or something else.

Every ``diagnostics.resource_sample_seconds`` the machine's available memory
and swap are read. Only while either is past its threshold is a sample
recorded: one ``memory_pressure`` diagnostic for the machine (swap, available
memory, the backbone's own memory, all agents' and everything else's) and one
``agent_memory`` diagnostic for each of the agents using the most. A healthy
machine records nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import re
import uuid
from typing import TYPE_CHECKING

from agent_backbone.services.terminal import process_memory

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB

TOP_AGENTS = 10
"""Agents recorded per sample, largest first: the sample stays bounded."""

_MB = 1024 * 1024
_SWAP = re.compile(r"total = ([\d.]+)M\s+used = ([\d.]+)M")


async def _sysctl(*names: str) -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        "sysctl", "-n", *names, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        async with asyncio.timeout(5):
            out, _ = await proc.communicate()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()
    return out.decode(errors="replace").splitlines()


def _meminfo(text: str) -> dict[str, int]:
    """``/proc/meminfo`` in MB."""
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        number = rest.split()[0] if rest.split() else ""
        if number.isdigit():
            values[key] = int(number) // 1024
    return values


async def machine_memory() -> dict[str, int] | None:
    """``memory_total_mb``, ``available_mb``, ``swap_total_mb`` and
    ``swap_used_mb``; None where this platform's figures can't be read."""
    system = platform.system()
    if system == "Linux":
        with open("/proc/meminfo", encoding="ascii") as fh:
            info = _meminfo(fh.read())
        if "MemTotal" not in info or "MemAvailable" not in info:
            return None
        return {
            "memory_total_mb": info["MemTotal"],
            "available_mb": info["MemAvailable"],
            "swap_total_mb": info.get("SwapTotal", 0),
            "swap_used_mb": info.get("SwapTotal", 0) - info.get("SwapFree", 0),
        }
    if system == "Darwin":
        # kern.memorystatus_level: the percentage of memory the system counts as available.
        lines = await _sysctl("hw.memsize", "kern.memorystatus_level", "vm.swapusage")
        swap = _SWAP.search(lines[2]) if len(lines) == 3 else None
        if swap is None or not lines[0].isdigit() or not lines[1].isdigit():
            return None
        total = int(lines[0]) // _MB
        return {
            "memory_total_mb": total,
            "available_mb": total * int(lines[1]) // 100,
            "swap_total_mb": int(float(swap.group(1))),
            "swap_used_mb": int(float(swap.group(2))),
        }
    return None


async def sample_resources(config: BackboneConfig, db: BackboneDB) -> dict:
    """Record one sample if memory is under pressure; say why not otherwise."""
    machine = await machine_memory()
    if machine is None:
        return {"sampled": False, "reason": "unsupported"}
    swap_limit = config.settings.get("diagnostics.pressure_swap_percent", 50)
    free_limit = config.settings.get("diagnostics.pressure_free_percent", 10)
    swap_used = machine["swap_used_mb"] * 100 // max(machine["swap_total_mb"], 1)
    available = machine["available_mb"] * 100 // max(machine["memory_total_mb"], 1)
    if (not machine["swap_total_mb"] or swap_used < swap_limit) and available > free_limit:
        return {"sampled": False, "reason": "no_pressure"}
    sessions, own_kb, total_kb = await process_memory()
    details = {**machine, "backbone_rss_mb": own_kb // 1024}
    agents = {}
    if sessions is not None:  # else tmux could not be read: agents' share unknown
        agents = {name: usage for name, usage in sessions.items() if name in config.agents.names}
        agents_kb = sum(kb for kb, _ in agents.values())
        details["agents_rss_mb"] = agents_kb // 1024
        details["other_rss_mb"] = max(0, total_kb - agents_kb - own_kb) // 1024
    operation_id = uuid.uuid4().hex
    await db.diagnostics.record(
        operation_id=operation_id,
        category="resource",
        code="memory_pressure",
        severity="warning",
        source="resource-sample",
        details=details,
    )
    largest = sorted(agents.items(), key=lambda item: item[1][0], reverse=True)[:TOP_AGENTS]
    for name, (kb, count) in largest:
        spec = config.agents.get(name)
        await db.diagnostics.record(
            operation_id=operation_id,
            category="resource",
            code="agent_memory",
            observation_key=name,
            severity="info",
            source="resource-sample",
            agent_name=name,
            runtime=spec.runtime if spec else "",
            details={"rss_mb": kb // 1024, "processes": count},
        )
    return {"sampled": True, "agents": len(largest)}
