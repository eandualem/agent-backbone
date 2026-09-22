"""Requesting a transition: validated before anything is torn down, then persisted."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from agent_backbone.config import BackboneSection
from agent_backbone.services.agents.transitions import (
    DEFAULT_DELAY_SECONDS,
    MAX_DELAY_SECONDS,
    TransitionPending,
    TransitionRequest,
    due_after,
    parse_start_at,
    request_transition,
    validate_transition,
)

_RUNTIME = "agent_backbone.services.runtimes.base.Runtime"


@pytest.fixture(autouse=True)
def _binaries():
    with patch(f"{_RUNTIME}.available", return_value=True):
        yield


def _spec(config, name="ike"):
    spec = config.agents.get(name)
    spec.path.mkdir(parents=True, exist_ok=True)
    return spec


class TestValidate:
    def test_defaults_pass(self, config):
        validate_transition(config, _spec(config), TransitionRequest())

    def test_refuses_the_backbones_own_session(self, config):
        config = replace(config, backbone=BackboneSection(session_name="ike"))
        with pytest.raises(ValueError, match="backbone's own session"):
            validate_transition(config, _spec(config), TransitionRequest())

    def test_cross_cli_resume_is_rejected_clearly(self, config):
        with pytest.raises(ValueError, match="same CLI.*runs claude.*codex was requested"):
            validate_transition(
                config, _spec(config), TransitionRequest(runtime="codex", resume=True)
            )

    def test_same_cli_resume_passes(self, config):
        validate_transition(config, _spec(config), TransitionRequest(runtime="claude", resume=True))

    def test_unknown_or_missing_runtime(self, config):
        with pytest.raises(ValueError, match="Unknown runtime"):
            validate_transition(config, _spec(config), TransitionRequest(runtime="nope"))
        with patch(f"{_RUNTIME}.available", return_value=False):
            with pytest.raises(ValueError, match="binary not found"):
                validate_transition(config, _spec(config), TransitionRequest(runtime="codex"))

    def test_effort_is_checked_against_the_replacement_runtime(self, config):
        with pytest.raises(ValueError, match="no effort 'turbo'"):
            validate_transition(config, _spec(config), TransitionRequest(model="opus:turbo"))
        with pytest.raises(ValueError, match="names an effort but no model"):
            validate_transition(config, _spec(config), TransitionRequest(model=":high"))
        validate_transition(config, _spec(config), TransitionRequest(model="opus:high"))

    def test_timing_rules(self, config):
        spec = _spec(config)
        with pytest.raises(ValueError, match="not both"):
            validate_transition(
                config,
                spec,
                TransitionRequest(delay_seconds=5, start_at="2030-01-01T00:00:00.000000Z"),
            )
        with pytest.raises(ValueError, match="between 0 and"):
            validate_transition(config, spec, TransitionRequest(delay_seconds=-1))
        with pytest.raises(ValueError, match="30 days"):
            validate_transition(
                config, spec, TransitionRequest(delay_seconds=MAX_DELAY_SECONDS + 1)
            )
        validate_transition(config, spec, TransitionRequest(delay_seconds=MAX_DELAY_SECONDS))
        with pytest.raises(ValueError, match="stop-only has no start"):
            validate_transition(config, spec, TransitionRequest(start=False, delay_seconds=5))
        validate_transition(config, spec, TransitionRequest(start=False))

    def test_stop_only_skips_runtime_checks(self, config):
        with patch(f"{_RUNTIME}.available", return_value=False):
            validate_transition(config, _spec(config), TransitionRequest(start=False))

    def test_missing_directory(self, config, tmp_path):
        spec = replace(config.agents.get("ike"), dir=str(tmp_path / "gone"))
        with pytest.raises(ValueError, match="Directory does not exist"):
            validate_transition(config, spec, TransitionRequest())


class TestRequest:
    async def test_persists_pending_with_the_default_delay(self, db, config):
        row = await request_transition(db, config, _spec(config), TransitionRequest(model="opus"))
        assert row["status"] == "pending" and row["delay_seconds"] == DEFAULT_DELAY_SECONDS
        assert row["model"] == "opus" and row["start"] is True and row["stopped_at"] is None
        assert await db.transitions.open_for("ike") == row

    async def test_one_open_transition_per_agent(self, db, config):
        first = await request_transition(db, config, _spec(config), TransitionRequest())
        with pytest.raises(TransitionPending) as exc:
            await request_transition(db, config, _spec(config), TransitionRequest())
        assert exc.value.row["id"] == first["id"]
        assert f"#{first['id']}" in str(exc.value)

    async def test_rejected_requests_leave_nothing_behind(self, db, config):
        with pytest.raises(ValueError):
            await request_transition(
                db, config, _spec(config), TransitionRequest(runtime="codex", resume=True)
            )
        assert await db.transitions.pending() == []


class TestTime:
    def test_parse_start_at_accepts_offsets_and_local_time(self):
        assert parse_start_at("2030-01-01T10:00:00+02:00") == "2030-01-01T08:00:00.000000Z"
        local = parse_start_at("2030-01-01T10:00:00")
        assert local.endswith("Z") and len(local) == len("2030-01-01T08:00:00.000000Z")
        with pytest.raises(ValueError, match="not an ISO 8601 time"):
            parse_start_at("tomorrow-ish")

    def test_due_after(self):
        stopped = datetime(2030, 1, 1, 0, 0, tzinfo=UTC)
        assert due_after(stopped, 3 * 3600 + 20 * 60) == "2030-01-01T03:20:00.000000Z"
