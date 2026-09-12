"""Accounting tests use numeric fixtures; no services or real user transcripts."""

import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.hooks.backbone_state import remember_usage_session
from agent_backbone.services.agents import collect_usage, usage_view
from agent_backbone.services.runtimes import RUNTIMES, read_usage_jsonl
from agent_backbone.usage import DEFAULT_PRICES, UsageEvent, estimate, timestamp
from tests.conftest import make_config

T = "2026-09-12T10:00:00Z"
T2 = "2026-09-12T10:01:00Z"


@pytest.fixture(autouse=True)
def isolated_sources(tmp_path, monkeypatch):
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))


def claude(message="m1", at=T, output=20, model="claude-opus-5"):
    return dict(
        type="assistant",
        timestamp=at,
        message=dict(
            id=message,
            model=model,
            usage=dict(
                input_tokens=100,
                cache_read_input_tokens=1000,
                cache_creation_input_tokens=200,
                output_tokens=output,
                cache_creation={"ephemeral_1h_input_tokens": 100},
            ),
        ),
    )


def codex(total=100, output=20, at=T, cached=50, last=None):
    values = dict(
        input_tokens=total,
        cached_input_tokens=cached,
        output_tokens=output,
        reasoning_output_tokens=5,
        cache_write_input_tokens=0,
    )
    return dict(
        type="event_msg",
        timestamp=at,
        payload=dict(
            type="token_count", info=dict(total_token_usage=values, last_token_usage=last or values)
        ),
    )


def append(path, *records):
    with path.open("a") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def config_for(tmp_path, runtime="claude"):
    config = make_config(
        tmp_path,
        agents=AgentsConfig(
            specs={"worker": AgentSpec(name="worker", dir=str(tmp_path), runtime=runtime)}
        ),
    )
    return config


def register(config, runtime="claude", sid="s1", launch=None):
    remember_usage_session(
        config.state_dir,
        "worker",
        dict(runtime=runtime, session_id=sid, ts=1789207200, launch_id=launch),
    )


async def test_request_revisions_resume_and_cli_switch_are_not_double_counted(
    tmp_path, db, monkeypatch
):
    config = config_for(tmp_path)
    transcript = tmp_path / "s1.jsonl"
    append(transcript, claude(output=1), claude(at=T2), claude(at=T2))
    monkeypatch.setattr(
        RUNTIMES["claude"], "usage_paths", lambda sid, env: [transcript] if sid == "s1" else []
    )
    register(config, launch="launch1")
    await collect_usage(config, db)
    await collect_usage(config, db)
    register(config, launch="launch2")
    await collect_usage(config, db)
    events = await db.usage.events()
    assert len(events) == 1 and events[0]["output_tokens"] == 20
    assert events[0]["input_tokens"] == 100 and events[0]["cache_read_tokens"] == 1000
    assert len((await db.usage.sessions())[0]["launches"]) == 2
    # Same agent, new CLI: a separate conversation, never retroactive relabeling.
    other = tmp_path / "s2.jsonl"
    append(
        other, dict(type="turn_context", payload=dict(model="gpt-6-astra", turn_id="t2")), codex()
    )
    monkeypatch.setattr(RUNTIMES["codex"], "usage_paths", lambda sid, env: [other])
    register(config, "codex", "s2")
    switched = replace(
        config,
        agents=AgentsConfig(
            specs={"worker": replace(config.agents.get("worker"), runtime="codex")}
        ),
    )
    with patch("agent_backbone.services.terminal.session_exists", AsyncMock(return_value=False)):
        view = await usage_view(switched, db)
    assert {s["runtime"] for s in view["sessions"]} == {"claude", "codex"}
    assert view["totals"]["total_tokens"] == 1320 + 120
    assert len(view["items"]) == 2


def test_codex_repeated_cumulative_events_and_model_change(tmp_path):
    path = tmp_path / "c.jsonl"
    append(
        path,
        dict(type="turn_context", payload=dict(model="gpt-6-astra", turn_id="t1")),
        codex(),
        codex(),
        dict(type="turn_context", payload=dict(model="other", turn_id="t2")),
        codex(
            total=300,
            output=50,
            cached=150,
            at=T2,
            last=dict(input_tokens=200, cached_input_tokens=100, output_tokens=30),
        ),
    )
    batch = read_usage_jsonl(path, 0, {}, RUNTIMES["codex"].parse_usage)
    assert len(batch.events) == 2
    assert [e.total_tokens for e in batch.events] == [120, 230]
    assert [e.model for e in batch.events] == ["gpt-6-astra", "other"]
    assert batch.events[0].reasoning_tokens == 5  # already within output
    assert (
        read_usage_jsonl(path, batch.offset, batch.state, RUNTIMES["codex"].parse_usage).events
        == []
    )


def test_partial_line_is_retried_and_reset_is_explicit(tmp_path):
    path = tmp_path / "c.jsonl"
    append(path, codex())
    line = json.dumps(codex(total=200, at=T2))
    with path.open("a") as f:
        f.write(line[:20])
    batch = read_usage_jsonl(path, 0, {}, RUNTIMES["codex"].parse_usage)
    assert len(batch.events) == 1 and not batch.caught_up
    with path.open("a") as f:
        f.write(line[20:] + "\n")
    next_batch = read_usage_jsonl(path, batch.offset, batch.state, RUNTIMES["codex"].parse_usage)
    assert next_batch.caught_up and next_batch.events[0].input_tokens == 100
    append(path, codex(total=80, cached=10, at="2026-09-12T10:02:00Z"))
    reset = read_usage_jsonl(
        path, next_batch.offset, next_batch.state, RUNTIMES["codex"].parse_usage
    )
    assert reset.state["partial"] and reset.events[0].coverage == "partial"


def test_cost_accounts_for_cache_duration_and_reasoning_once():
    event = UsageEvent(
        key="a",
        at=T,
        model="claude-opus-5",
        input_tokens=1000000,
        cache_read_tokens=1000000,
        cache_write_tokens=2000000,
        cache_write_1h_tokens=1000000,
        output_tokens=1000000,
        reasoning_tokens=500000,
    )
    assert event.total_tokens == 5000000
    assert Decimal(estimate(event, DEFAULT_PRICES)["usd"]) == Decimal("46.75")
    unknown = event.model_copy(update={"model": "unknown"})
    assert estimate(unknown, DEFAULT_PRICES)["usd"] is None


def test_long_context_rate_and_missing_context():
    event = UsageEvent(
        key="a",
        at=T,
        model="gpt-6-astra",
        input_tokens=300000,
        output_tokens=1000,
        context_tokens=300000,
    )
    assert Decimal(estimate(event, DEFAULT_PRICES)["usd"]) == Decimal("6.075")
    assert (
        estimate(event.model_copy(update={"context_tokens": None}), DEFAULT_PRICES)["usd"] is None
    )


async def test_child_usage_and_time_filtered_pagination(tmp_path, db, monkeypatch):
    config = config_for(tmp_path)
    path = tmp_path / "s1.jsonl"
    append(path, claude())
    children = tmp_path / "s1" / "subagents"
    children.mkdir(parents=True)
    append(children / "agent-child.jsonl", claude("child", T2, output=40, model="claude-sonnet-5"))
    monkeypatch.setattr(
        RUNTIMES["claude"], "usage_paths", lambda sid, env: [path] if sid == "s1" else []
    )
    register(config)
    with patch("agent_backbone.services.terminal.session_exists", AsyncMock(return_value=False)):
        view = await usage_view(config, db, limit=1)
        assert view["has_more"] and view["totals"]["observations"] == 2
        child = next(s for s in view["sessions"] if s["parent_id"])
        assert child["total_tokens"] == 1340
        filtered = await usage_view(config, db, since=T2, refresh=False)
        assert filtered["totals"]["total_tokens"] == 1340
        models = await usage_view(config, db, by="model", refresh=False)
        assert len(models["items"]) == 2


async def test_cursor_and_events_commit_once_and_pricing_is_reproducible(tmp_path, db):
    key = await db.usage.remember("worker", "claude", "s", at=T)
    session = (await db.usage.sessions())[0]
    event = UsageEvent(key="request", at=T, model="claude-opus-5", input_tokens=1000000)
    args = dict(
        path=str(tmp_path / "x"),
        offset=10,
        cursor={"x": 1},
        events=[event],
        coverage="measured",
        detail="",
        prices=DEFAULT_PRICES,
        observed_at=timestamp(T),
    )
    assert await db.usage.ingest(session, **args)
    assert not await db.usage.ingest(session, **args)
    current = (await db.usage.sessions())[0]
    revised = event.model_copy(update={"at": T2, "output_tokens": 1000000})
    prices = {**DEFAULT_PRICES, "claude-opus-5": {**DEFAULT_PRICES["claude-opus-5"], "input": 999}}
    assert await db.usage.ingest(
        current, **{**args, "offset": 20, "events": [revised], "prices": prices}
    )
    row = (await db.usage.events(session=key))[0]
    assert Decimal(row["cost"]["usd"]) == 30
    assert row["at"] == T
    with pytest.raises(ValueError):
        await db.usage.remember("different", "claude", "s", at=T)


def test_opencode_reasoning_and_cache_are_counted_once(tmp_path):
    import sqlite3

    path = tmp_path / "opencode.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE message(id TEXT, session_id TEXT, time_updated INTEGER, data TEXT)"
        )
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?)",
            (
                "m",
                "s",
                1789207200000,
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "example",
                        "time": {"created": 1789207200000},
                        "tokens": {
                            "total": 10867,
                            "input": 9965,
                            "output": 100,
                            "reasoning": 561,
                            "cache": {"read": 241, "write": 0},
                        },
                    }
                ),
            ),
        )
    batch = RUNTIMES["opencode"].read_usage(path, 0, {"_session_id": "s"})
    assert batch.error is None
    assert batch.events[0].total_tokens == 10867
    assert batch.events[0].output_tokens == 661
    assert batch.events[0].reasoning_tokens == 561
    assert RUNTIMES["opencode"].read_usage(path, 0, {"_session_id": "other"}).events == []


async def test_persistence_after_restart_and_partial_correction(tmp_path):
    from agent_backbone.services.database import BackboneDB

    url = f"sqlite+aiosqlite:///{tmp_path / 'history.db'}"
    async with BackboneDB.connect(url) as database:
        await database.usage.remember("worker", "claude", "s", at=T)
        session = (await database.usage.sessions())[0]
        event = UsageEvent(key="m", at=T, model="claude-opus-5", output_tokens=100)
        assert await database.usage.ingest(
            session,
            path="missing",
            offset=5,
            cursor={},
            events=[event],
            coverage="measured",
            detail="",
            prices=DEFAULT_PRICES,
            observed_at=T,
        )
    async with BackboneDB.connect(url) as database:
        current = (await database.usage.sessions())[0]
        assert current["offset"] == 5
        # A later authoritative correction can decrease a counter; don't fabricate a max.
        revision = event.model_copy(update={"at": T2, "output_tokens": 90})
        assert await database.usage.ingest(
            current,
            path="missing",
            offset=10,
            cursor={},
            events=[revision],
            coverage="measured",
            detail="",
            prices=DEFAULT_PRICES,
            observed_at=T2,
        )
        assert (await database.usage.events())[0]["output_tokens"] == 90


async def test_current_session_uses_runtime_observation_not_configured_cli(
    tmp_path, db, monkeypatch
):
    from agent_backbone.services.agents import AgentState, StateSnapshot

    config = config_for(tmp_path, "claude")
    await db.usage.remember("worker", "codex", "s1", at=T)
    with (
        patch("agent_backbone.services.terminal.session_exists", AsyncMock(return_value=True)),
        patch(
            "agent_backbone.services.agents._inference.agent_state",
            AsyncMock(
                return_value=StateSnapshot(state=AgentState.BUSY, runtime="codex", session_id="s1")
            ),
        ),
    ):
        await db.usage.remember("worker", "claude", "old", at=T)
        view = await usage_view(config, db, refresh=False, current_only=True)
    assert len(view["sessions"]) == 1
    assert view["sessions"][0]["current"] is True
    assert view["sessions"][0]["runtime"] == "codex"
    assert view["sessions"][0]["coverage"] == "unavailable"
    assert view["sessions"][0]["estimated_usd"] is None


async def test_usage_api_auth_validation_and_payload(api_client, auth_headers):
    assert (await api_client.get("/api/usage?refresh=false")).status_code == 401
    api_client.headers.update(auth_headers)
    response = await api_client.get("/api/usage?refresh=false")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == [] and data["totals"]["estimated_usd"] is None
    assert (await api_client.get("/api/usage?limit=0")).status_code == 422
    assert (await api_client.get("/api/usage?since=not-a-time")).status_code == 422
    assert (await api_client.get("/api/usage?runtime=unknown")).status_code == 422


def test_codex_fork_inherited_baseline_is_not_new_spend(tmp_path):
    path = tmp_path / "fork.jsonl"
    append(
        path,
        dict(type="session_meta", payload=dict(timestamp=T)),
        codex(
            total=10000,
            output=500,
            cached=9000,
            last=dict(input_tokens=100, cached_input_tokens=50, output_tokens=20),
        ),
    )
    batch = read_usage_jsonl(path, 0, {}, RUNTIMES["codex"].parse_usage)
    assert batch.events[0].total_tokens == 120
    assert batch.state["partial"]


def test_codex_child_discovery_uses_explicit_lineage(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    child = root / "child.jsonl"
    append(
        child,
        dict(
            type="session_meta",
            payload=dict(
                id="child", source={"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
            ),
        ),
    )
    unrelated = root / "unrelated.jsonl"
    append(unrelated, dict(type="session_meta", payload=dict(id="other", source="cli")))
    assert RUNTIMES["codex"].usage_children(
        root / "parent.jsonl", "parent", {"CODEX_HOME": str(tmp_path)}
    ) == [("child", child)]


def test_claude_copied_conversation_history_is_excluded():
    record = {**claude(), "sessionId": "old"}
    assert RUNTIMES["claude"].parse_usage(record, {"_session_id": "new"}) is None
    assert RUNTIMES["claude"].parse_usage(record, {"_session_id": "old/agent-child"}) is not None


def test_missing_cache_duration_is_unpriced_but_tokens_retained():
    record = claude()
    del record["message"]["usage"]["cache_creation"]
    event = RUNTIMES["claude"].parse_usage(record, {})
    assert event.total_tokens == 1320
    assert estimate(event, DEFAULT_PRICES)["usd"] is None


def test_opencode_large_history_makes_progress_across_equal_timestamps(tmp_path):
    import sqlite3

    path = tmp_path / "opencode.db"
    encoded = json.dumps(
        {"role": "assistant", "time": {"created": 1789207200000}, "tokens": {"input": 1}}
    )
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE message(id TEXT,session_id TEXT,time_updated INTEGER,data TEXT)")
        c.executemany(
            "INSERT INTO message VALUES (?,?,?,?)",
            [(f"m{i:05}", "s", 1789207200000, encoded) for i in range(10002)],
        )
    first = RUNTIMES["opencode"].read_usage(path, 0, {"_session_id": "s"})
    assert first.error is None and not first.caught_up and len(first.events) == 10000
    second = RUNTIMES["opencode"].read_usage(path, first.offset, first.state)
    assert second.caught_up and len(second.events) == 2
    assert not {e.key for e in first.events} & {e.key for e in second.events}
