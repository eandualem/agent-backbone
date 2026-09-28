"""Each runtime reads its own transcript into the agent's complete user-facing messages."""

from __future__ import annotations

import json
import sqlite3

from agent_backbone.services.runtimes import get_runtime
from agent_backbone.services.runtimes.base import transcript_clock
from tests.support import opencode_db

LONG = "A" * 5000 + "\nsecond paragraph " + "B" * 3000


def test_clock():
    assert transcript_clock("2026-09-22T16:50:54.123Z") == "16:50:54"
    assert transcript_clock(None) == "" and transcript_clock("nope") == ""


def test_claude_keeps_only_assistant_text_complete():
    records = [
        {"type": "ai-title", "aiTitle": "x", "_start": 0, "_end": 10},
        {
            "type": "user",
            "timestamp": "2026-09-22T10:00:00.000Z",
            "message": {"role": "user", "content": "Fix the bug"},
            "_start": 10,
            "_end": 20,
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-22T10:00:05.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "Looking at it now."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
                ],
            },
            "_start": 20,
            "_end": 30,
        },
        {
            "type": "user",
            "timestamp": "2026-09-22T10:00:09.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "3 passed"}],
            },
            "_start": 30,
            "_end": 40,
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-22T10:00:12.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": LONG}]},
            "_start": 40,
            "_end": 9000,
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {"content": [{"type": "text", "text": "sub-agent"}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
        },
    ]
    entries = get_runtime("claude").transcript_entries(records)
    assert [(e.time, e.role, e.start, e.end) for e in entries] == [
        ("10:00:05", "assistant", 20, 30),
        ("10:00:12", "assistant", 40, 9000),
    ]
    assert entries[0].text == "Looking at it now."
    assert entries[1].text == LONG  # never shortened


def test_codex_keeps_assistant_messages_complete_and_nothing_else():
    records = [
        {"type": "session_meta", "payload": {"id": "s"}},
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:00.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "think"}]}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": "ls"},
        },
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "a.py"}},
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:03.000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Checking the lock first."}],
            },
            "_start": 100,
            "_end": 200,
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:09.000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": LONG}],
            },
            "_start": 200,
            "_end": 9000,
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "developer", "content": "sys"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "not a response item"},
        },
    ]
    entries = get_runtime("codex").transcript_entries(records)
    assert [(e.time, e.text[:8], e.start, e.end) for e in entries] == [
        ("18:00:03", "Checking", 100, 200),
        ("18:00:09", "AAAAAAAA", 200, 9000),
    ]
    assert entries[1].text == LONG


def _opencode_text(body, ended=1790071205000):
    return {"type": "text", "text": body, "time": {"start": ended - 900, "end": ended}}


def test_opencode_keeps_completed_assistant_text_complete_by_position(tmp_path):
    base = 1790071200000  # each part below is created a millisecond after the one before
    assistant = {"role": "assistant"}
    path = opencode_db(
        tmp_path / "opencode.db",
        [
            ("s", {"role": "user"}, [{"type": "text", "text": "Fix the bug"}]),
            (
                "s",
                assistant,
                [
                    {"type": "step-start"},
                    {**_opencode_text("Looking at it now."), "_created": base + 2},  # same ms
                    {"type": "tool", "tool": "bash", "state": {"output": "3 passed"}},
                    {"type": "step-finish"},
                ],
            ),
            (
                "s",
                assistant,
                [
                    {"type": "reasoning", "text": "private"},
                    _opencode_text(LONG, 1790071212000),
                    _opencode_text(" \n"),
                ],
            ),
            ("s", {"role": "assistant", "summary": True}, [_opencode_text("compaction")]),
            ("child", assistant, [_opencode_text("sub-agent")]),
            ("s", assistant, [{"type": "text", "text": "", "time": {"start": base}}]),  # streaming
        ],
    )
    entries, more_before, more_after = get_runtime("opencode").transcript_page(path, "s", limit=10)
    assert [(e.time, e.role, e.start, e.end) for e in entries] == [
        ("10:00:05", "assistant", (base + 2) * 1000 + 1, (base + 2) * 1000 + 2),
        ("10:00:12", "assistant", (base + 6) * 1000, (base + 6) * 1000 + 1),
    ]
    assert entries[0].text == "Looking at it now."
    assert entries[1].text == LONG  # never shortened
    assert more_before is False and more_after is False


def test_opencode_positions_survive_an_undo_that_frees_rowids(tmp_path):
    assistant = {"role": "assistant"}
    path = opencode_db(
        tmp_path / "opencode.db",
        [("s", assistant, [_opencode_text("kept")]), ("s", assistant, [_opencode_text("undone")])],
    )
    rt = get_runtime("opencode")
    seen, _, _ = rt.transcript_page(path, "s", limit=10)
    # Undo deletes the last message; SQLite gives its rowid to the next reply.
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("DELETE FROM part WHERE message_id = 'msg_1'")
        conn.execute("DELETE FROM message WHERE id = 'msg_1'")
        conn.execute("INSERT INTO message VALUES ('msg_2', 's', 0, 0, ?)", (json.dumps(assistant),))
        conn.execute(
            "INSERT INTO part VALUES ('prt_2_0', 'msg_2', 's', 1790071300000, 0, ?)",
            (json.dumps(_opencode_text("after the undo")),),
        )
        assert conn.execute("SELECT rowid FROM part WHERE id = 'prt_2_0'").fetchone() == (2,)
    conn.close()
    later, more_before, more_after = rt.transcript_page(path, "s", limit=10, since=seen[-1].end)
    assert [e.text for e in later] == ["after the undo"]
    assert more_before is True and more_after is False


def test_other_runtimes_keep_no_readable_transcript():
    for runtime in ("gemini", "aider", "shell"):
        rt = get_runtime(runtime)
        assert rt.transcript_supported is False
        assert rt.transcript_entries([{"type": "assistant", "message": {"content": "x"}}]) == []
