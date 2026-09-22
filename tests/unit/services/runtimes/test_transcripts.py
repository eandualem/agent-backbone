"""Each runtime reads its own transcript into the agent's complete user-facing messages."""

from __future__ import annotations

from agent_backbone.services.runtimes import get_runtime
from agent_backbone.services.runtimes.base import transcript_clock

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


def test_other_runtimes_keep_no_readable_transcript():
    for runtime in ("gemini", "opencode", "aider", "shell"):
        rt = get_runtime(runtime)
        assert rt.transcript_supported is False
        assert rt.transcript_entries([{"type": "assistant", "message": {"content": "x"}}]) == []
