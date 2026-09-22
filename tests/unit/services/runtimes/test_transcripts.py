"""Each runtime reads its own transcript format into plain, bounded entries."""

from __future__ import annotations

import json

from agent_backbone.services.runtimes import get_runtime
from agent_backbone.services.runtimes.base import (
    transcript_argument,
    transcript_clip,
    transcript_clock,
)


def test_helpers_clip_summarise_and_tell_the_time():
    assert transcript_clock("2026-09-22T16:50:54.123Z") == "16:50:54"
    assert transcript_clock(None) == "" and transcript_clock("nope") == ""
    assert transcript_clip("  first line\nsecond") == "first line …"
    assert transcript_clip("x" * 300).endswith("…") and len(transcript_clip("x" * 300)) == 200
    assert transcript_clip("") == "" and transcript_clip(None) == ""
    assert transcript_argument({"command": "ls -la", "description": "List files"}) == "List files"
    assert transcript_argument({"file_path": "/a/b.py"}) == "/a/b.py"
    assert transcript_argument('{"cmd": ["git", "status"]}') == "git status"
    assert transcript_argument("plain text") == "plain text"
    assert transcript_argument({"n": 3}) == '{"n": 3}'
    assert transcript_argument(None) == ""


def test_claude_entries_skip_thinking_and_bookkeeping():
    records = [
        {"type": "ai-title", "aiTitle": "x"},
        {
            "type": "user",
            "timestamp": "2026-09-22T10:00:00.000Z",
            "message": {"role": "user", "content": "Fix the bug\nplease"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-22T10:00:05.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "Looking."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-22T10:00:09.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "3 passed\n", "is_error": False},
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": ""}],
                        "is_error": True,
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {"content": [{"type": "text", "text": "sub"}]},
        },
    ]
    entries = get_runtime("claude").transcript_entries(records)
    assert [(e.time, e.role, e.text) for e in entries] == [
        ("10:00:00", "user", "Fix the bug …"),
        ("10:00:05", "assistant", "Looking."),
        ("10:00:05", "tool", "Bash pytest -q"),
        ("10:00:09", "result", "3 passed"),
        ("10:00:09", "result", "error"),
    ]


def test_codex_entries_read_messages_calls_and_outputs():
    output = json.dumps([{"type": "input_text", "text": "Script completed\nWall time 1s"}])
    records = [
        {"type": "session_meta", "payload": {"id": "s"}},
        {"type": "event_msg", "payload": {"type": "token_count"}},
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:00.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
        {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:01.000Z",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "arguments": '{"cmd": "git status"}',
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:02.000Z",
            "payload": {"type": "custom_tool_call_output", "call_id": "c", "output": output},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:03.000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "developer", "content": "sys"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:04.000Z",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "text(await tools.ls())",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:05.000Z",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [
                    {"type": "input_text", "text": "a.py\nb.py"},
                    {"type": "input_text", "text": "{}"},
                ],
            },
        },
    ]
    entries = get_runtime("codex").transcript_entries(records)
    assert [(e.time, e.role, e.text) for e in entries] == [
        ("18:00:00", "user", "hi"),
        ("18:00:01", "tool", "exec git status"),
        ("18:00:02", "result", "Script completed …"),
        ("18:00:03", "assistant", "Done."),
        ("18:00:04", "tool", "exec text(await tools.ls())"),
        ("18:00:05", "result", "a.py …"),
    ]


def test_other_runtimes_keep_no_readable_transcript():
    for runtime in ("gemini", "opencode", "aider", "shell"):
        rt = get_runtime(runtime)
        assert rt.transcript_supported is False
        assert rt.transcript_entries([{"type": "user", "message": {"content": "x"}}]) == []
