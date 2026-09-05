"""Provider failures are visible, queue deliveries, and disappear after recovery."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.agents import (
    AgentState,
    get_agent_state,
    infer_state_from_pane,
    write_state_file,
)
from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.runtimes import RUNTIMES

CASES = [
    ("codex", "Selected model is at capacity. Please try again later.", "›"),
    (
        "opencode",
        "You exceeded your current quota: generate_content_free_tier_requests, limit: 20",
        "Ask anything...",
    ),
    ("claude", "You've hit your limit · resets at 3 PM", "❯"),
]


@pytest.mark.parametrize("runtime,error,prompt", CASES)
def test_current_provider_banner_is_blocked(runtime, error, prompt):
    snapshot = infer_state_from_pane(
        f"\x1b[31m{error}\x1b[0m\nPlease retry in 49s\n{prompt}", runtime
    )
    assert snapshot.state == AgentState.BLOCKED and snapshot.reason == "provider"
    assert error in snapshot.detail and "49s" in snapshot.detail
    assert error in " ".join(snapshot.evidence)


@pytest.mark.parametrize("runtime,error,prompt", CASES)
def test_old_errors_and_quoted_examples_do_not_block(runtime, error, prompt):
    rt = RUNTIMES[runtime]
    banner = f"\x1b[31m{error}\x1b[0m"
    assert rt.provider_failure(f"{banner}\nCompleted the requested change.\n{prompt}") is None
    assert rt.provider_failure(f'Example: "{error}"\n{prompt}') is None
    assert rt.provider_failure(f"{banner}\nRunning tests now · esc to interrupt\n{prompt}") is None


@pytest.mark.parametrize(
    "runtime,error,prompt",
    CASES
    + [
        ("codex", "Error: Selected model is at capacity", "›"),
        ("codex", "You've hit your usage limit", "›"),
        ("codex", "Rate limit reached", "›"),
        ("codex", "Too many requests", "›"),
        ("codex", "insufficient_quota", "›"),
        ("claude", "API Error: 429", "❯"),
        ("claude", "Credit balance is too low", "❯"),
        ("claude", "Rate limit exceeded", "❯"),
        ("opencode", "Rate limit exceeded", "Ask anything..."),
        ("opencode", "RESOURCE_EXHAUSTED", "Ask anything..."),
        ("opencode", "Selected model is at capacity", "Ask anything..."),
    ],
)
async def test_unquoted_response_is_not_a_provider_banner(tmp_path, runtime, error, prompt):
    write_state_file(tmp_path, "app", {"state": "idle", "ts": time.time()})
    result = await get_agent_state(
        tmp_path, "app", runtime_hint=runtime, pane_content=f"{error}\n{prompt}"
    )
    assert result.state == AgentState.IDLE


@pytest.mark.parametrize("style", ["31", "91", "38;5;196", "38;2;240;80;80"])
def test_provider_error_foreground_formats(style):
    assert RUNTIMES["opencode"].provider_failure(f"\x1b[{style}m{CASES[1][1]}\x1b[0m")


@pytest.mark.parametrize("style", ["32", "48;5;196", "31;0", "38;2;50;200;200"])
def test_non_error_foregrounds_do_not_count(style):
    assert RUNTIMES["opencode"].provider_failure(f"\x1b[{style}m{CASES[1][1]}\x1b[0m") is None


@pytest.mark.parametrize("state,age", [("busy", 600), ("idle", 0)])
async def test_error_overrides_stale_busy_or_fresh_idle(tmp_path, state, age):
    write_state_file(
        tmp_path, "app", {"state": state, "ts": time.time() - age, "issue": 5, "repo": "acme/app"}
    )
    result = await get_agent_state(
        tmp_path, "app", runtime_hint="codex", pane_content="■ " + CASES[0][1] + "\n›"
    )
    assert result.state == AgentState.BLOCKED and result.reason == "provider"
    assert (result.current_issue, result.current_repo) == (5, "acme/app")
    recovered = await get_agent_state(
        tmp_path,
        "app",
        runtime_hint="codex",
        pane_content="■ " + CASES[0][1] + "\nCompleted successfully.\n›",
    )
    assert recovered.state == AgentState.IDLE


async def test_fresh_busy_hook_remains_authoritative(tmp_path):
    write_state_file(tmp_path, "app", {"state": "busy", "ts": time.time()})
    result = await get_agent_state(
        tmp_path, "app", runtime_hint="codex", pane_content="■ " + CASES[0][1] + "\n›"
    )
    assert result.state == AgentState.BUSY


async def test_provider_block_queues_even_priority_delivery(config, db):
    intelligence = "agent_backbone.services.routing._intelligence"
    with (
        patch(f"{intelligence}.list_sessions", AsyncMock(return_value=["ike"])),
        patch(f"{intelligence}.capture_pane", AsyncMock(return_value="■ " + CASES[0][1] + "\n›")),
        patch(f"{intelligence}.resolve_runtime", AsyncMock(return_value=RUNTIMES["codex"])),
        patch("agent_backbone.services.routing._delivery.send_message", AsyncMock()) as send,
    ):
        await safe_deliver(
            "ike",
            "new assignment",
            config,
            db=db,
            priority=True,
            delivery_kind="direct_message",
        )
    send.assert_not_awaited()
    assert await db.queue.pending_count("ike") == 1
