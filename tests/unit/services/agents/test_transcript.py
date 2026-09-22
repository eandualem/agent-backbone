"""``agent output``: a bounded tail of the runtime's transcript, else the screen."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.agents import write_state_file
from agent_backbone.services.agents.transcript import (
    MAX_ENTRIES,
    output_tail,
    read_transcript_tail,
)
from agent_backbone.services.runtimes import get_runtime

_MOD = "agent_backbone.services.agents.transcript"


def _claude_record(index: int, text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": f"2026-09-22T10:00:{index % 60:02d}.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def _transcript(tmp_path, count: int, *, name="s1.jsonl", pad: str = ""):
    path = tmp_path / name
    path.write_text("".join(_claude_record(i, f"reply {i}{pad}") + "\n" for i in range(count)))
    return path


class TestReadTail:
    def test_last_entries_and_a_cursor_to_continue_from(self, tmp_path):
        path = _transcript(tmp_path, 5)
        lines, cursor = read_transcript_tail(path, get_runtime("claude"), lines=2, since=None)
        assert lines == ["10:00:03 assistant: reply 3", "10:00:04 assistant: reply 4"]
        assert cursor == path.stat().st_size
        # Nothing new: the cursor stays; something appended: only that comes back.
        assert read_transcript_tail(path, get_runtime("claude"), lines=2, since=cursor) == (
            [],
            cursor,
        )
        with path.open("a") as fh:
            fh.write(_claude_record(9, "reply 9") + "\n")
        lines, moved = read_transcript_tail(path, get_runtime("claude"), lines=2, since=cursor)
        assert lines == ["10:00:09 assistant: reply 9"] and moved == path.stat().st_size

    def test_a_forward_read_stops_at_a_complete_record(self, tmp_path):
        path = _transcript(tmp_path, 3)
        with patch(f"{_MOD}.TAIL_BYTES", 150):  # smaller than two records
            lines, cursor = read_transcript_tail(path, get_runtime("claude"), lines=10, since=0)
        assert lines == ["10:00:00 assistant: reply 0"]
        text = path.read_bytes()
        assert text[:cursor].endswith(b"\n") and text[cursor:].startswith(b'{"type"')

    def test_walks_back_in_steps_but_never_past_the_bound(self, tmp_path):
        path = _transcript(tmp_path, 40, pad="x" * 100)
        rt = get_runtime("claude")
        with patch(f"{_MOD}.TAIL_BYTES", 400):
            lines, _ = read_transcript_tail(path, rt, lines=6, since=None)
            assert [f"reply {i}" in line for i, line in zip(range(34, 40), lines)] == [True] * 6
            with patch(f"{_MOD}.MAX_TAIL_BYTES", 500):
                bounded, _ = read_transcript_tail(path, rt, lines=30, since=None)
        assert 0 < len(bounded) < 30  # bounded recent content, never the whole file

    def test_malformed_lines_are_skipped(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text("not json\n" + _claude_record(1, "ok") + '\n{"type": 3}\n')
        lines, _ = read_transcript_tail(path, get_runtime("claude"), lines=5, since=None)
        assert lines == ["10:00:01 assistant: ok"]


@pytest.fixture
def live():
    with (
        patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True) as exists,
        patch(
            f"{_MOD}.query_environment_var", new_callable=AsyncMock, return_value="claude"
        ) as env,
        patch(
            f"{_MOD}.capture_pane",
            new_callable=AsyncMock,
            return_value="\x1b[1m❯\x1b[0m hello\n\n",
        ) as pane,
    ):
        yield exists, env, pane


class TestOutputTail:
    async def test_transcript_located_from_the_hooks_session_id(self, config, tmp_path, live):
        path = _transcript(tmp_path, 3, name="abc.jsonl")
        write_state_file(
            config.state_dir, "ike", {"state": "idle", "session_id": "abc", "runtime": "claude"}
        )
        with patch(
            "agent_backbone.services.runtimes.claude.ClaudeCode.usage_paths", return_value=[path]
        ):
            tail = await output_tail(config, "ike", lines=2)
        assert tail.source == "transcript" and tail.runtime == "claude"
        assert tail.lines == ["10:00:01 assistant: reply 1", "10:00:02 assistant: reply 2"]
        assert tail.cursor == path.stat().st_size
        assert tail.evidence == [f"transcript {path}"]
        live[2].assert_not_awaited()

    async def test_screen_when_there_is_no_transcript_and_why(self, config, live):
        tail = await output_tail(config, "ike", lines=5)
        assert tail.source == "screen" and tail.cursor is None
        assert tail.lines == ["❯ hello"]  # ANSI stripped, trailing blanks dropped
        assert tail.evidence == ["no session id recorded by the hook yet"]

    async def test_a_session_id_from_another_runtime_is_not_used(self, config, live):
        write_state_file(
            config.state_dir, "ike", {"state": "idle", "session_id": "abc", "runtime": "codex"}
        )
        tail = await output_tail(config, "ike")
        assert tail.source == "screen"
        assert tail.evidence == ["the recorded session id belongs to codex, not claude"]

    async def test_unsupported_runtime_and_forced_screen(self, config, live):
        live[1].return_value = "shell"
        tail = await output_tail(config, "ike")
        assert tail.source == "screen" and tail.runtime == "shell"
        assert tail.evidence == ["shell keeps no transcript the backbone can read"]
        forced = await output_tail(config, "ike", screen=True)
        assert forced.source == "screen" and forced.evidence == ["screen requested"]

    async def test_offline_agent_has_no_screen(self, config, live):
        live[0].return_value = False
        tail = await output_tail(config, "ike")
        assert tail.source == "screen" and tail.lines == [] and "offline" in tail.evidence

    async def test_entries_are_bounded(self, config, live, tmp_path):
        path = _transcript(tmp_path, 3, name="abc.jsonl")
        write_state_file(
            config.state_dir, "ike", {"state": "idle", "session_id": "abc", "runtime": "claude"}
        )
        with patch(
            "agent_backbone.services.runtimes.claude.ClaudeCode.usage_paths", return_value=[path]
        ):
            tail = await output_tail(config, "ike", lines=MAX_ENTRIES * 10)
        assert len(tail.lines) == 3
