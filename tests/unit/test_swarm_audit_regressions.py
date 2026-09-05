"""Regressions for defects the 2026-09-05 swarm audit found.

See docs/reviews/2026-09-05-swarm-audit.md for the findings and their dispositions.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import validate_setting
from agent_backbone.hooks.backbone_state import issue_from_prompt, issue_from_text
from agent_backbone.services.agents import read_state_file
from agent_backbone.services.terminal._core import paste_message

_CORE = "agent_backbone.services.terminal._core"


class TestIssueFromPrompt:
    """S2-4: the first number in a prompt is not the issue."""

    def test_number_after_hash_wins_over_an_earlier_bare_number(self):
        assert issue_from_text("Review 2 changes for issue #42") == (42, None)

    def test_envelope_issue_colon_form(self):
        assert issue_from_text("[via:github issue:42] New comment") == (42, None)

    def test_bare_number_without_the_word_issue_is_not_an_issue(self):
        assert issue_from_text("bump timeout to 30") == (None, None)
        assert issue_from_prompt("bump timeout to 30", {"issue": 7, "repo": "a/b"}) == (7, "a/b")

    def test_qualified_reference_still_wins(self):
        assert issue_from_text("see acme/app#7 and issue #9") == (7, "acme/app")


class TestSettingsLists:
    """S2-3: a list setting with a non-string member would break every later build_config."""

    def test_list_members_must_be_strings(self):
        assert validate_setting("routing.ignore_targets", ["bob"]) == ["bob"]
        with pytest.raises(ValueError, match="list of strings"):
            validate_setting("routing.ignore_targets", [{}])


class TestStateFileShapes:
    """S2-9: valid JSON of the wrong shape degrades like unreadable JSON."""

    @pytest.mark.parametrize("body", ["[]", '{"ts": "bad", "state": "busy"}', '"busy"'])
    def test_malformed_state_falls_back(self, tmp_path, body):
        (tmp_path / "ike.json").write_text(body)
        assert read_state_file(tmp_path, "ike") is None

    def test_well_formed_state_still_reads(self, tmp_path):
        (tmp_path / "ike.json").write_text(json.dumps({"state": "busy", "ts": 5.0, "issue": 3}))
        snap = read_state_file(tmp_path, "ike")
        assert snap is not None and snap.current_issue == 3


class TestPasteBuffer:
    """S2-1: each paste uses its own tmux buffer; S2-2: targets are exact."""

    async def test_named_buffer_is_loaded_pasted_and_target_exact(self):
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as run:
            run.return_value = (0, b"", b"")
            assert await paste_message("ike", "hello") is True
        calls = [c.args for c in run.await_args_list]
        assert calls[0] == ("has-session", "-t", "=ike")
        load, paste = calls[1], calls[2]
        assert load[:3] == ("load-buffer", "-b", load[2]) and load[2].startswith("backbone-")
        assert paste == ("paste-buffer", "-p", "-b", load[2], "-t", "=ike", "-d")

    async def test_failed_paste_deletes_its_buffer(self):
        answers = iter([(0, b"", b""), (0, b"", b""), (1, b"", b"nope"), (0, b"", b"")])
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as run:
            run.side_effect = lambda *a, **k: next(answers)
            assert await paste_message("ike", "hello") is False
        assert run.await_args_list[-1].args[0] == "delete-buffer"


class TestNonFiniteTimestamps:
    @pytest.mark.parametrize("ts", ['"inf"', '"-inf"', '"nan"'])
    def test_are_rejected(self, tmp_path, ts):
        (tmp_path / "ike.json").write_text(f'{{"state": "busy", "ts": {ts}}}')
        assert read_state_file(tmp_path, "ike") is None

    @pytest.mark.parametrize("ts", ['"inf"', '"nan"'])
    def test_a_non_finite_starting_marker_is_ignored(self, tmp_path, ts):
        (tmp_path / "ike.starting").write_text(f'{{"ts": {ts}}}')
        assert read_state_file(tmp_path, "ike") is None
