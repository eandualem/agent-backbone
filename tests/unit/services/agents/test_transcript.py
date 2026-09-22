"""``agent output``: complete messages a page at a time, with offsets to go either way."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.services.agents import write_state_file
from agent_backbone.services.agents.transcript import (
    MAX_MESSAGES,
    output_page,
    read_messages,
)
from agent_backbone.services.runtimes import get_runtime

_MOD = "agent_backbone.services.agents.transcript"
_CLAUDE = "agent_backbone.services.runtimes.claude.ClaudeCode.usage_paths"


def _assistant(index: int, text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": f"2026-09-22T10:00:{index % 60:02d}.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def _tool_noise(index: int, size: int = 200) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "x" * size}]},
            "n": index,
        }
    )


def _transcript(tmp_path, count: int, *, name="s1.jsonl", noise: int = 200, texts=None):
    path = tmp_path / name
    lines = []
    for i in range(count):
        lines.append(_tool_noise(i, noise))
        lines.append(_assistant(i, texts[i] if texts else f"reply {i}"))
    path.write_text("\n".join(lines) + "\n")
    return path


def _texts(messages):
    return [m.text for m in messages]


class TestReadMessages:
    def test_last_page_then_back_then_forward_loses_nothing(self, tmp_path):
        path = _transcript(tmp_path, 7)
        rt = get_runtime("claude")
        last, more_before, more_after, _ = read_messages(path, rt, limit=3)
        assert _texts(last) == ["reply 4", "reply 5", "reply 6"]
        assert more_before is True and more_after is False
        assert last[-1].end == path.stat().st_size

        earlier, more_before, more_after, _ = read_messages(path, rt, limit=3, before=last[0].start)
        assert _texts(earlier) == ["reply 1", "reply 2", "reply 3"]
        assert more_before is True and more_after is True

        first, more_before, _, _ = read_messages(path, rt, limit=3, before=earlier[0].start)
        assert _texts(first) == ["reply 0"] and more_before is False

        forward, more_before, more_after, _ = read_messages(path, rt, limit=2, since=first[-1].end)
        assert _texts(forward) == ["reply 1", "reply 2"]
        assert more_before is True and more_after is True
        # A start/end range, and continuing from a cursor that has nothing after it.
        ranged, _, more_after, _ = read_messages(
            path, rt, limit=10, since=earlier[0].start, end=earlier[-1].end
        )
        assert _texts(ranged) == ["reply 1", "reply 2", "reply 3"] and more_after is False
        assert read_messages(path, rt, limit=5, since=last[-1].end)[0] == []

    def test_a_long_message_is_returned_whole(self, tmp_path):
        long = "L" * 300_000 + "\n" + "M" * 300_000
        path = _transcript(tmp_path, 3, texts=["a", long, "c"])
        rt = get_runtime("claude")
        with patch(f"{_MOD}.STEP_BYTES", 4096):  # the record is far longer than a step
            page, _, _, _ = read_messages(path, rt, limit=3)
            assert _texts(page) == ["a", long, "c"]
            forward, _, _, _ = read_messages(path, rt, limit=3, since=0)
            assert _texts(forward) == ["a", long, "c"]

    def test_the_scan_bound_is_reported_not_silent(self, tmp_path):
        path = _transcript(tmp_path, 20, noise=5000)
        rt = get_runtime("claude")
        with patch(f"{_MOD}.STEP_BYTES", 4096), patch(f"{_MOD}.MAX_SCAN_BYTES", 20_000):
            page, more_before, _, evidence = read_messages(path, rt, limit=50)
        assert 0 < len(page) < 20 and more_before is True
        assert evidence and evidence[0].startswith("scan bound reached")

    def test_a_record_past_the_scan_bound_is_refused_not_loaded(self, tmp_path):
        path = _transcript(tmp_path, 3, texts=["a", "L" * 50_000, "c"])
        rt = get_runtime("claude")
        with patch(f"{_MOD}.STEP_BYTES", 4096), patch(f"{_MOD}.MAX_SCAN_BYTES", 20_000):
            with pytest.raises(ValueError, match="exceeds the scan bound"):
                read_messages(path, rt, limit=3)
            with pytest.raises(ValueError, match="exceeds the scan bound"):
                read_messages(path, rt, limit=3, since=0)

    def test_malformed_lines_are_skipped(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text("not json\n" + _assistant(1, "ok") + '\n{"type": 3}\n')
        page, _, _, _ = read_messages(path, get_runtime("claude"), limit=5)
        assert _texts(page) == ["ok"]


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


class TestOutputPage:
    async def test_transcript_located_from_the_hooks_session_id(self, config, tmp_path, live):
        path = _transcript(tmp_path, 3, name="abc.jsonl")
        write_state_file(
            config.state_dir, "ike", {"state": "idle", "session_id": "abc", "runtime": "claude"}
        )
        with patch(_CLAUDE, return_value=[path]):
            page = await output_page(config, "ike", limit=2)
        assert page.source == "transcript" and page.runtime == "claude"
        assert _texts(page.messages) == ["reply 1", "reply 2"]
        assert page.range_start == page.messages[0].start
        assert page.range_end == page.messages[-1].end == path.stat().st_size
        assert page.more_before is True and page.more_after is False
        assert page.evidence == [f"transcript {path}"]
        live[2].assert_not_awaited()

    async def test_a_candidate_that_vanishes_is_skipped(self, config, tmp_path, live):
        path = _transcript(tmp_path, 3, name="abc.jsonl")
        gone = MagicMock(**{"is_file.return_value": True, "stat.side_effect": FileNotFoundError})
        write_state_file(
            config.state_dir, "ike", {"state": "idle", "session_id": "abc", "runtime": "claude"}
        )
        with patch(_CLAUDE, return_value=[gone, path]):
            page = await output_page(config, "ike")
        assert page.source == "transcript" and page.evidence == [f"transcript {path}"]

    async def test_screen_when_there_is_no_transcript_and_why(self, config, live):
        page = await output_page(config, "ike", limit=5)
        assert page.source == "screen" and page.range_end is None and page.messages == []
        assert page.lines == ["❯ hello"]  # ANSI stripped, trailing blanks dropped
        assert page.evidence == ["no session id recorded by the hook yet"]

    async def test_a_session_id_from_another_runtime_is_not_used(self, config, live):
        write_state_file(
            config.state_dir, "ike", {"state": "idle", "session_id": "abc", "runtime": "codex"}
        )
        page = await output_page(config, "ike")
        assert page.source == "screen"
        assert page.evidence == ["the recorded session id belongs to codex, not claude"]

    async def test_unsupported_runtime_and_forced_screen(self, config, live):
        live[1].return_value = "shell"
        page = await output_page(config, "ike")
        assert page.source == "screen" and page.runtime == "shell"
        assert page.evidence == ["shell keeps no transcript the backbone can read"]
        forced = await output_page(config, "ike", screen=True)
        assert forced.source == "screen" and forced.evidence == ["screen requested"]

    async def test_offline_agent_has_no_screen(self, config, live):
        live[0].return_value = False
        page = await output_page(config, "ike")
        assert page.source == "screen" and page.lines == [] and "offline" in page.evidence

    async def test_pages_are_bounded(self, config, live, tmp_path):
        path = _transcript(tmp_path, 3, name="abc.jsonl")
        write_state_file(
            config.state_dir, "ike", {"state": "idle", "session_id": "abc", "runtime": "claude"}
        )
        with patch(_CLAUDE, return_value=[path]):
            page = await output_page(config, "ike", limit=MAX_MESSAGES * 10)
        assert len(page.messages) == 3
