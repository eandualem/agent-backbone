"""Performance evidence: slow job runs and memory samples under pressure."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.jobs import resources
from agent_backbone.services.jobs.diagnostics import observe_duration
from agent_backbone.services.jobs.resources import machine_memory, sample_resources

_RES = "agent_backbone.services.jobs.resources"


async def test_a_slow_episode_is_one_record_with_its_longest_run(db):
    await observe_duration(db, source="agent-monitor", duration_ms=1_000, threshold_ms=30_000)
    assert await db.diagnostics.query() == []
    for duration in (45_000, 90_000, 31_000):
        await observe_duration(
            db, source="agent-monitor", duration_ms=duration, threshold_ms=30_000
        )
    (slow,) = await db.diagnostics.query()
    assert (slow["code"], slow["severity"], slow["source"]) == (
        "run_slow",
        "warning",
        "agent-monitor",
    )
    assert slow["occurrences"] == 3 and slow["details"]["duration_ms"] == 90_000
    await observe_duration(db, source="agent-monitor", duration_ms=2_000, threshold_ms=30_000)
    await observe_duration(db, source="agent-monitor", duration_ms=40_000, threshold_ms=30_000)
    assert len(await db.diagnostics.query()) == 2  # an ordinary run ended the first episode
    digest = await db.diagnostics.digest()
    assert [(job["job"], job["longest_ms"]) for job in digest["slow_jobs"]] == [
        ("agent-monitor", 40_000),
        ("agent-monitor", 90_000),
    ]


async def test_a_slow_episode_keeps_the_steps_of_its_longest_run(db):
    for duration, step in ((45_000, "queue_drain"), (90_000, "pending_issues"), (31_000, "x")):
        await observe_duration(
            db,
            source="agent-monitor",
            duration_ms=duration,
            threshold_ms=30_000,
            steps={step: duration - 1_000, "not a code": 1, "usage_collection": "slow"},
        )
    (slow,) = await db.diagnostics.query()
    assert slow["details"]["steps"] == {"pending_issues": 89_000}  # only codes and counters


async def test_a_threshold_of_zero_records_no_slow_run(db):
    await observe_duration(db, source="prune", duration_ms=600_000, threshold_ms=0)
    assert await db.diagnostics.query() == []


_HEALTHY = {
    "memory_total_mb": 16384,
    "available_mb": 8000,
    "swap_total_mb": 2048,
    "swap_used_mb": 0,
}
_SWAPPING = {**_HEALTHY, "available_mb": 6000, "swap_total_mb": 23552, "swap_used_mb": 22130}


async def test_a_healthy_machine_records_nothing(db, config):
    with (
        patch(f"{_RES}.machine_memory", AsyncMock(return_value=_HEALTHY)),
        patch(f"{_RES}.process_memory", AsyncMock()) as processes,
    ):
        assert await sample_resources(config, db) == {"sampled": False, "reason": "no_pressure"}
    processes.assert_not_awaited()
    assert await db.diagnostics.query() == []


@pytest.mark.parametrize(
    "machine", [_SWAPPING, {**_HEALTHY, "available_mb": 1000, "swap_total_mb": 0}]
)
async def test_under_pressure_the_machine_and_its_largest_agents_are_recorded(db, config, machine):
    sizes = range(100, 1000, 100)  # MB, one per agent
    sessions = {name: (mb * 1024, 3) for name, mb in zip(config.agents.names, sizes, strict=True)}
    sessions["viewer"] = (9_000_000, 1)  # not an agent: counted with everything else
    with (
        patch(f"{_RES}.machine_memory", AsyncMock(return_value=machine)),
        patch(f"{_RES}.process_memory", AsyncMock(return_value=(sessions, 51_200, 20_000_000))),
        patch(f"{_RES}.TOP_AGENTS", 3),
    ):
        assert await sample_resources(config, db) == {"sampled": True, "agents": 3}
    memory = (await db.diagnostics.digest())["memory"]
    assert memory["samples"] == 1 and memory["swap_used_mb"] == machine["swap_used_mb"]
    assert memory["backbone_rss_mb"] == 50 and memory["agents_rss_mb"] == 4500  # 100 + … + 900
    assert memory["other_rss_mb"] == (20_000_000 - 4500 * 1024 - 51_200) // 1024
    assert [agent["rss_mb"] for agent in memory["agents"]] == [900, 800, 700]
    assert memory["agents"][0]["processes"] == 3


async def test_macos_memory_is_read_from_sysctl():
    lines = [
        "17179869184",
        "37",
        "total = 22528.00M  used = 21969.12M  free = 558.88M  (encrypted)",
    ]
    with (
        patch(f"{_RES}.platform.system", return_value="Darwin"),
        patch(f"{_RES}._sysctl", AsyncMock(return_value=lines)),
    ):
        assert await machine_memory() == {
            "memory_total_mb": 16384,
            "available_mb": 6062,
            "swap_total_mb": 22528,
            "swap_used_mb": 21969,
        }


def test_linux_memory_is_read_from_proc_meminfo():
    text = (
        "MemTotal: 16384000 kB\nMemAvailable: 2048000 kB\n"
        "SwapTotal: 1024000 kB\nSwapFree: 24576 kB\n"
    )
    info = resources._meminfo(text)
    assert info["MemTotal"] == 16000 and info["SwapTotal"] - info["SwapFree"] == 976


async def test_unknown_agent_memory_is_not_recorded_as_none(db, config):
    with (
        patch(f"{_RES}.machine_memory", AsyncMock(return_value=_SWAPPING)),
        patch(f"{_RES}.process_memory", AsyncMock(return_value=(None, 51_200, 20_000_000))),
    ):
        assert await sample_resources(config, db) == {"sampled": True, "agents": 0}
    memory = (await db.diagnostics.digest())["memory"]
    assert "agents_rss_mb" not in memory and "other_rss_mb" not in memory
    assert memory["backbone_rss_mb"] == 50


async def test_an_agents_digest_lists_only_that_agent_and_no_jobs(db, config):
    await observe_duration(db, source="agent-monitor", duration_ms=45_000, threshold_ms=30_000)
    sessions = dict.fromkeys(config.agents.names, (100 * 1024, 1))
    with (
        patch(f"{_RES}.machine_memory", AsyncMock(return_value=_SWAPPING)),
        patch(f"{_RES}.process_memory", AsyncMock(return_value=(sessions, 0, 20_000_000))),
    ):
        await sample_resources(config, db)
    name = (
        config.agents.names[0]
        if isinstance(config.agents.names, list)
        else sorted(config.agents.names)[0]
    )
    digest = await db.diagnostics.digest(agent_name=name)
    assert digest["slow_jobs"] == []
    assert [agent["agent_name"] for agent in digest["memory"]["agents"]] == [name]
    assert digest["memory"]["swap_used_mb"] == _SWAPPING["swap_used_mb"]  # machine context stays


async def test_a_long_slow_job_list_says_it_is_cut_off(db):
    for _ in range(3):  # three episodes: each ends with an ordinary run
        await observe_duration(db, source="prune", duration_ms=40_000, threshold_ms=30_000)
        await observe_duration(db, source="prune", duration_ms=1_000, threshold_ms=30_000)
    digest = await db.diagnostics.digest(limit=2)
    assert len(digest["slow_jobs"]) == 2 and digest["slow_jobs_more"]


async def test_a_failed_process_read_keeps_the_machine_figures(db, config):
    with (
        patch(f"{_RES}.machine_memory", AsyncMock(return_value=_SWAPPING)),
        patch(f"{_RES}.process_memory", AsyncMock(side_effect=TimeoutError)),
    ):
        assert await sample_resources(config, db) == {"sampled": True, "agents": 0}
    memory = (await db.diagnostics.digest())["memory"]
    assert memory["swap_used_mb"] == _SWAPPING["swap_used_mb"]
    assert "backbone_rss_mb" not in memory and "agents_rss_mb" not in memory
