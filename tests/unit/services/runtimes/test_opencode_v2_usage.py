"""OpenCode v2's projected native rows retain usage and assistant text."""

import json
import sqlite3

import pytest

from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.usage import timestamp


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


@pytest.mark.parametrize("legacy_first", [False, True])
async def test_mixed_formats_keep_native_output_and_all_usage(tmp_path, db, legacy_first):
    path = tmp_path / "opencode.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT);"
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT);"
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE part (id TEXT, session_id TEXT, message_id TEXT, "
            "time_created INTEGER, data TEXT);"
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
            "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);"
        )
        for table, child in [("session", "legacy-child"), ("session_v2", "native-child")]:
            conn.executemany(
                f"INSERT INTO {table} VALUES (?,?)",
                [("root", None), (child, "root"), ("shared-child", "root")],
            )
        for key, clock, tokens in [("common", 1000, 5), ("legacy", 2000, 2), ("summary", 500, 4)]:
            data = {
                "role": "assistant",
                "time": {"created": clock},
                "tokens": {"input": tokens},
                "summary": key == "summary",
            }
            conn.execute(
                "INSERT INTO message VALUES (?,?,?,?,?)",
                (key, "root", clock, clock, json.dumps(data)),
            )
            part = {"type": "text", "text": key, "time": {"end": clock + 10}}
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                (key, "root", key, clock + 1, json.dumps(part)),
            )
        for seq, (key, clock, tokens) in enumerate([("common", 1000, 8), ("native", 3000, 3)]):
            data = {
                "model": {"id": "model", "providerID": "provider"},
                "time": {"created": clock, "completed": clock + 10},
                "tokens": {"input": tokens},
                "content": [{"type": "text", "text": key}],
            }
            conn.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                (key, "root", "assistant", seq, clock, clock, json.dumps(data)),
            )
        # Migrated compactions contain summary text but no summary inference usage.
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (
                "compaction",
                "root",
                "compaction",
                2,
                500,
                500,
                json.dumps({"time": {"created": 500}, "summary": "summary"}),
            ),
        )
        for table in ("message", "session_message"):
            conn.execute(f"UPDATE {table} SET time_updated=2500 WHERE id='common'")
    runtime = RUNTIMES["opencode"]
    identity = await db.usage.remember("worker", "opencode", "root", at=timestamp(1))

    async def ingest(batch):
        session = await db.usage.session(identity)
        assert await db.usage.ingest(
            session,
            path=str(path),
            offset=batch.offset,
            cursor=batch.state,
            events=batch.events,
            coverage="measured",
            detail="",
            prices={},
            observed_at=timestamp(4),
        )

    assert runtime.usage_children(path, "root", {}) == [
        (child, path) for child in ["legacy-child", "native-child", "shared-child"]
    ]
    # Adopt the merged reader from an old checkpoint which missed legacy rows.
    batch = runtime.read_usage(
        path, 3000, {"_session_id": "root", "scan_floor": 3000, "scan_after": (3000, "native")}
    )
    assert batch.error is None and batch.caught_up
    assert {event.key: event.input_tokens for event in batch.events} == {
        "common": 8,
        "legacy": 2,
        "native": 3,
        "summary": 4,
    }
    if legacy_first:
        # Previously collected v1 usage must gain the v2 priority on UPDATE,
        # not only when the selected native copy is inserted for the first time.
        legacy = next(event for event in batch.events if event.key == "common").model_copy(
            update={"input_tokens": 5}
        )
        legacy._revision_priority = 0
        initial = runtime.read_usage(path, 0, {"_session_id": "root"})
        initial.events = [legacy]
        await ingest(initial)
    await ingest(batch)
    with pytest.raises(ValueError, match="ambiguous"):
        runtime.transcript_page(path, "root", limit=1)
    for version, last_text in [("opencode-v1", "legacy"), ("opencode-v2", "native")]:
        page, before, after = runtime.transcript_page(
            path, "root", limit=1, transcript_format=version
        )
        assert [entry.text for entry in page] == [last_text]
        assert before and not after
        earlier, before, after = runtime.transcript_page(
            path, "root", limit=1, before=page[0].start, transcript_format=version
        )
        assert [entry.text for entry in earlier] == ["common"]
        assert not before and after
        forward, _, after = runtime.transcript_page(
            path, "root", limit=1, since=earlier[0].end, transcript_format=version
        )
        assert [entry.text for entry in forward] == [last_text] and not after
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM session_message WHERE type='assistant'")

    # Native revert must not resurrect legacy copies in the current conversation.
    page, before, after = runtime.transcript_page(
        path, "root", limit=20, transcript_format="opencode-v2"
    )
    assert page == [] and not before and not after
    replay = runtime.read_usage(path, batch.offset, batch.state)
    assert replay.error is None
    await ingest(replay)
    stored = {row["key"]: row["input_tokens"] for row in await db.usage.events(session=identity)}
    assert stored == {"common": 8, "legacy": 2, "native": 3, "summary": 4}
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE message SET time_updated=4000, "
            "data=json_set(data, '$.tokens.input', 12) WHERE id='common'"
        )
    revised = runtime.read_usage(path, replay.offset, replay.state)
    assert revised.error is None
    await ingest(revised)
    stored = {row["key"]: row["input_tokens"] for row in await db.usage.events(session=identity)}
    assert stored["common"] == 12
    assert {event.key: event.input_tokens for event in revised.events} == {
        "common": 12,
        "legacy": 2,
    }


def test_usage_ranks_raw_copies_before_parsing_and_skips_bad_observations(tmp_path):
    path = tmp_path / "opencode.db"
    valid = json.dumps({"role": "assistant", "time": {"created": 2000}, "tokens": {"input": 2}})
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE message (id TEXT, session_id TEXT, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_message (id TEXT, session_id TEXT, time_updated INTEGER, "
            "type TEXT, data TEXT);"
        )
        conn.execute("INSERT INTO message VALUES ('common','s',2000,?)", (valid,))
        conn.executemany(
            "INSERT INTO session_message VALUES (?, 's', ?, 'assistant', ?)",
            [("bad", 500, "not json"), ("common", 1000, "older invalid copy")],
        )
    runtime = RUNTIMES["opencode"]
    first = runtime.read_usage(path, 0, {"_session_id": "s"})
    assert first.error and first.state["partial"] and not first.caught_up
    second = runtime.read_usage(path, first.offset, first.state)
    assert second.error is None and second.caught_up and second.state["partial"]
    assert [(event.key, event.input_tokens) for event in second.events] == [("common", 2)]
