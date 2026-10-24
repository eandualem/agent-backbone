"""OpenCode v2's projected native rows retain usage and assistant text."""

import json
import sqlite3

import pytest

from agent_backbone.services.runtimes import RUNTIMES
from tests.support import opencode_session_table


def test_v2_usage_children_and_completed_transcript(tmp_path):
    root = tmp_path / "opencode"
    root.mkdir()
    path = root / "opencode.db"
    clock = 1790071205000
    message = {
        "model": {"id": "model", "providerID": "provider", "variant": "high"},
        "time": {"created": clock, "completed": clock + 10},
        "tokens": {"input": 100, "output": 20, "reasoning": 10, "cache": {"read": 50}},
        "cost": 0.01,
        "content": [
            {"type": "text", "text": "First paragraph."},
            {"type": "reasoning", "text": "Private reasoning"},
            {"type": "text", "text": "Second paragraph."},
        ],
    }
    with sqlite3.connect(path) as conn:
        opencode_session_table(conn)
        conn.executescript(
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
            "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);"
        )
        conn.executemany(
            "INSERT INTO session_v2(id,parent_id) VALUES (?,?)", [("root", None), ("child", "root")]
        )
        for key, session, kind, seq, data in [
            ("answer", "root", "assistant", 2, message),
            ("summary", "root", "compaction", 3, message),
            ("child-answer", "child", "assistant", 4, message),
            ("stream", "root", "assistant", 5, {**message, "time": {"created": clock}}),
        ]:
            conn.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                (key, session, kind, seq, clock, clock + seq, json.dumps(data)),
            )
    runtime = RUNTIMES["opencode"]
    assert runtime.usage_paths("root", {"XDG_DATA_HOME": str(tmp_path)}) == [path]
    assert runtime.usage_children(path, "root", {}) == [("child", path)]
    batch = runtime.read_usage(path, 0, {"_session_id": "root"})
    assert batch.error is None
    assert batch.caught_up
    assert [event.key for event in batch.events] == ["answer", "summary", "stream"]
    answer = batch.events[0]
    assert (answer.model, answer.provider, answer.input_tokens, answer.output_tokens) == (
        "model",
        "provider",
        100,
        30,
    )
    assert answer.cache_read_tokens == 50
    entries, before, after = runtime.transcript_page(path, "root", limit=1)
    assert [entry.text for entry in entries] == ["Second paragraph."]
    assert before and not after
    earlier, before, after = runtime.transcript_page(path, "root", limit=1, before=entries[0].start)
    assert [entry.text for entry in earlier] == ["First paragraph."]
    assert not before and after


@pytest.mark.parametrize("total", [5, 9])
def test_native_totals_expose_missing_attribution_without_adding_usage(tmp_path, total):
    path = tmp_path / "opencode.db"
    with sqlite3.connect(path) as conn:
        opencode_session_table(conn)
        conn.execute(
            "INSERT INTO session_v2(id,tokens_input,fork_session_id,time_created) "
            "VALUES ('s',?,'parent',1000)",
            (total,),
        )
        conn.execute(
            "CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,"
            "time_created INTEGER,time_updated INTEGER,data TEXT)"
        )
        for key, created, kind, data in [
            ("inherited", 500, "assistant", {"tokens": {"input": 100}}),
            ("summary", 1500, "compaction", {"summary": "migrated summary"}),
            (
                "reply",
                2000,
                "assistant",
                {"model": {"id": "model"}, "time": {"created": 2000}, "tokens": {"input": 5}},
            ),
        ]:
            conn.execute(
                "INSERT INTO session_message VALUES (?, 's', ?, ?, ?, ?)",
                (key, kind, created, created, json.dumps(data)),
            )
    runtime = RUNTIMES["opencode"]
    batch = runtime.read_usage(path, 0, {"_session_id": "s"})
    assert batch.error is None and batch.caught_up
    assert [(e.key, e.input_tokens) for e in batch.events] == [("reply", 5)]
    assert bool(batch.state.get("coverage_detail")) is (total != 5)
    if total != 5:
        assert "attribution is incomplete" in batch.state["coverage_detail"]
    # Native undo removes visible messages without refunding incurred usage.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM session_message WHERE id='reply'")
    after = runtime.read_usage(path, batch.offset, batch.state)
    assert after.error is None and after.caught_up and after.state["coverage_detail"]
    assert after.events == []  # No invented request or duplicated ledger amount.

    # UsageRecorded precedes Compaction.Ended: tokens can arrive in the same
    # millisecond, without changing the native session or maximum message timestamp.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE session_message SET data=?,time_updated=1500 WHERE id='summary'",
            (json.dumps({"time": {"created": 1500}, "tokens": {"input": total}}),),
        )
    reconciled = runtime.read_usage(path, after.offset, after.state)
    assert reconciled.error is None and reconciled.caught_up
    assert not reconciled.state.get("coverage_detail")
    assert not reconciled.state.get("partial")
    assert [(e.key, e.input_tokens) for e in reconciled.events] == [("summary", total)]
    # A prior malformed record remains partial even if aggregate totals agree.
    damaged = runtime.read_usage(path, after.offset, {**after.state, "partial": True})
    assert damaged.state["partial"] and not damaged.state.get("coverage_detail")
