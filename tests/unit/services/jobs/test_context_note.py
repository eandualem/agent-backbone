"""The context-size note: offered once per level and conversation, through hook context."""

from __future__ import annotations

import time

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.hooks import backbone_state as bb
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.jobs.context_note import note_context_size
from agent_backbone.usage import UsageEvent
from tests.conftest import make_config
from tests.unit.hooks.test_context import _tool_call

T = "2026-10-13T10:00:00Z"


def _config(tmp_path, runtime="claude", tags=(), **settings):
    agents = AgentsConfig(
        specs={"desk": AgentSpec(name="desk", dir=str(tmp_path), runtime=runtime, tags=tags)}
    )
    return make_config(tmp_path, agents=agents, settings=settings)


def _states(runtime="claude", session="s"):
    snapshot = StateSnapshot(
        state=AgentState.BUSY,
        timestamp=time.time(),
        source="push",
        runtime=runtime,
        session_id=session,
    )
    return {"desk": snapshot}


async def _request(db, tokens, runtime="claude", session="s"):
    """One recorded request of the conversation, with its context size."""
    identity = await db.usage.remember("desk", runtime, session, at=T)
    row = await db.usage.session(identity)
    event = UsageEvent(key=f"r{row['offset']}", at=f"2026-10-13T10:{row['offset']:02d}:00Z")
    assert await db.usage.ingest(
        row,
        path="/x",
        offset=row["offset"] + 1,
        cursor={},
        events=[event.model_copy(update={"context_tokens": tokens})],
        coverage="measured",
        detail="",
        prices={},
        observed_at=T,
    )


def _offered(config) -> list[str]:
    return bb.take_context(config.state_dir, "desk")


async def test_the_first_note_comes_at_the_first_threshold(tmp_path, db):
    config = _config(tmp_path)
    await _request(db, 150_000)
    assert await note_context_size(config, db, _states()) == []
    await _request(db, 205_000)
    assert await note_context_size(config, db, _states()) == ["desk"]
    assert _offered(config) == [
        "[via:backbone context] Your conversation is at 205k tokens. When you finish your "
        "current task, write your handoff and start a fresh session (backbone agent restart)."
    ]


async def test_each_further_step_is_noted_once(tmp_path, db):
    config = _config(tmp_path)
    await _request(db, 210_000)
    await note_context_size(config, db, _states())
    await _request(db, 290_000)
    assert await note_context_size(config, db, _states()) == []
    await _request(db, 312_000)
    assert await note_context_size(config, db, _states()) == ["desk"]
    (text,) = _offered(config)  # the earlier note, not taken, is superseded
    assert "312k tokens" in text


async def test_a_level_is_never_noted_again_after_a_backbone_restart(tmp_path, db):
    config = _config(tmp_path)
    await _request(db, 230_000)
    assert await note_context_size(config, db, _states()) == ["desk"]
    assert len(_offered(config)) == 1
    # Nothing is kept in memory: the next tick, or a restarted backbone, reads the database.
    assert await note_context_size(config, db, _states()) == []
    assert _offered(config) == []


async def test_a_fresh_conversation_starts_over(tmp_path, db):
    config = _config(tmp_path)
    await _request(db, 320_000)
    await note_context_size(config, db, _states())
    assert _offered(config)
    await _request(db, 220_000, session="s2")
    assert await note_context_size(config, db, _states(session="s2")) == ["desk"]
    (text,) = _offered(config)
    assert "220k tokens" in text


async def test_swarm_members_are_not_noted(tmp_path, db):
    config = _config(tmp_path, tags=("swarm:review",))
    await _request(db, 400_000)
    assert await note_context_size(config, db, _states()) == []


@pytest.mark.parametrize("key", ["usage.context_note_tokens", "usage.enabled"])
async def test_the_note_can_be_turned_off(tmp_path, db, key):
    config = _config(tmp_path, **{key: 0 if key != "usage.enabled" else False})
    await _request(db, 400_000)
    assert await note_context_size(config, db, _states()) == []


async def test_a_step_of_zero_notes_only_once(tmp_path, db):
    config = _config(tmp_path, **{"usage.context_note_step_tokens": 0})
    await _request(db, 210_000)
    assert await note_context_size(config, db, _states()) == ["desk"]
    await _request(db, 900_000)
    assert await note_context_size(config, db, _states()) == []


async def test_a_runtime_without_hook_context_is_not_noted(tmp_path, db):
    config = _config(tmp_path, runtime="gemini")
    await _request(db, 400_000, runtime="gemini")
    assert await note_context_size(config, db, _states(runtime="gemini")) == []


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_the_runtimes_own_hook_hands_the_note_over(tmp_path, db, monkeypatch, runtime):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    config = _config(tmp_path, runtime=runtime)
    await _request(db, 250_000, runtime=runtime)
    assert await note_context_size(config, db, _states(runtime=runtime)) == ["desk"]
    assert _tool_call(runtime, config.state_dir).startswith(
        "[via:backbone context] Your conversation is at 250k tokens."
    )
    assert _tool_call(runtime, config.state_dir) == ""
