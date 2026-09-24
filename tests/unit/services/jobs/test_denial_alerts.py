"""Refusals without a dialog reach the humans once, safely, and are never replayed."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.recent import RecentKeys
from agent_backbone.services.jobs import escalation

REFUSAL = {
    "action": "permission_denied",
    "kind": "classifier",
    "category": "External System Writes",
    "tool": "Bash",
    "summary": "gh issue edit",
    "session": "ike",
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(escalation, "_denial_log_offset", None)
    monkeypatch.setattr(escalation, "_denial_notified", RecentKeys(1800))


def _append(config, *records, raw: str = ""):
    config.action_log_path.parent.mkdir(parents=True, exist_ok=True)
    with config.action_log_path.open("a") as log_file:
        for record in records:
            log_file.write(json.dumps(record) + "\n")
        log_file.write(raw)


async def _check(config) -> AsyncMock:
    with patch(
        "agent_backbone.services.jobs.escalation.notify_humans", AsyncMock(return_value=True)
    ) as notify:
        await escalation.check_permission_denials(config)
    return notify


async def test_refusals_before_the_first_check_are_never_replayed(config):
    _append(config, REFUSAL)
    assert not (await _check(config)).await_count  # the start of the watch
    _append(config, REFUSAL)
    notify = await _check(config)
    assert notify.await_count == 1


async def test_one_notice_names_the_action_and_says_it_cannot_be_approved_here(config):
    await _check(config)
    _append(config, REFUSAL, REFUSAL, {**REFUSAL, "summary": "gh api"})
    notify = await _check(config)
    assert notify.await_count == 2  # the repeat of the same action is folded
    text = notify.await_args_list[0].args[1]
    assert notify.await_args_list[0].kwargs == {"agent": "ike"}  # no buttons
    assert "Refused — ike" in text and "Action: gh issue edit" in text
    assert "auto-mode safety check refused it (External System Writes)" in text
    assert "cannot be approved from here" in text and "/permissions" in text
    assert "does not retry" in text


async def test_unknown_agents_other_actions_and_half_lines_are_skipped(config):
    await _check(config)
    _append(
        config,
        {**REFUSAL, "session": "stranger"},
        {"action": "comment", "session": "ike"},
        raw=json.dumps(REFUSAL)[:20],  # still being written
    )
    assert not (await _check(config)).await_count


async def test_the_first_refusals_in_a_new_log_are_reported(config):
    await _check(config)  # no log yet
    _append(config, REFUSAL)
    assert (await _check(config)).await_count == 1
