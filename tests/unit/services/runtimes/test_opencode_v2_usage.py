"""OpenCode v2's projected native rows retain usage and assistant text."""

import json
import sqlite3

from agent_backbone.services.runtimes import RUNTIMES


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
        conn.executescript(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT);"
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
            "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);"
        )
        conn.executemany("INSERT INTO session_v2 VALUES (?,?)", [("root", None), ("child", "root")])
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
