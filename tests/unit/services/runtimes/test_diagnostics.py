"""Typed terminal observations do not infer state or retain provider bodies."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_backbone.services.runtimes import RUNTIMES

PANE = (Path(__file__).parents[3] / "fixtures" / "codex-model-error.txt").read_text()


def test_model_account_error_survives_wrap_at_word_boundary():
    wrapped = PANE.replace("not supported", "not\nsupported")
    failure = next(
        signal for signal in RUNTIMES["codex"].diagnostics(wrapped) if signal.severity == "error"
    )
    assert failure.code == "model_account_incompatible"
    assert failure.model == "example-model" and failure.http_status == 400


def test_codex_records_error_and_later_model_change_without_inventing_recovery():
    signals = {item.code: item for item in RUNTIMES["codex"].diagnostics(PANE)}
    assert set(signals) == {"model_account_incompatible", "model_changed"}
    failure = signals["model_account_incompatible"]
    assert (failure.model, failure.error_type, failure.http_status) == (
        "example-model",
        "invalid_request_error",
        400,
    )
    assert failure.reason == "unsupported_model_for_account"
    changed = signals["model_changed"]
    assert (changed.severity, changed.model, changed.observed_effort) == (
        "info",
        "example-codex-model",
        "high",
    )
    # The ordinary state detector remains unchanged: a visible error is an
    # observation, not an instruction to declare this recovered or blocked.
    assert RUNTIMES["codex"].detect_idle(PANE)


def test_error_wrapped_inside_json_words_and_spaces_is_still_recognized():
    error = PANE.splitlines()[1]
    wrapped = "\n".join(error[index : index + 55] for index in range(0, len(error), 55))
    signals = RUNTIMES["codex"].diagnostics(wrapped + "\n›")
    assert len(signals) == 1 and signals[0].code == "model_account_incompatible"


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: "\n".join("> " + line for line in text.splitlines()),
        lambda text: "```text\n" + text + "\n```",
        lambda text: text.replace("■ ", "").replace("• ", ""),
    ],
)
def test_quoted_or_fenced_examples_are_not_runtime_observations(transform):
    assert RUNTIMES["codex"].diagnostics(transform(PANE)) == ()


@pytest.mark.parametrize(
    "replacement",
    [
        ('"status":400', '"status":500'),
        ('"type":"invalid_request_error"', '"type":"another_error"'),
        ("with a ChatGPT account.", "for another reason."),
    ],
)
def test_unknown_errors_retain_type_and_status_without_guessing_cause(replacement):
    banner = PANE.splitlines()[1].replace(*replacement)
    signals = RUNTIMES["codex"].diagnostics(banner)
    assert len(signals) == 1 and signals[0].code == "request_error"
    assert signals[0].reason is None and signals[0].model is None


@pytest.mark.parametrize(
    "replacement",
    [
        ('"status":400', '"status":"400"'),
        ('"status":400', '"status":200'),
        ('"type":"invalid_request_error"', '"type":"private error text"'),
    ],
)
def test_invalid_error_envelopes_are_not_recognized(replacement):
    banner = PANE.splitlines()[1].replace(*replacement)
    assert RUNTIMES["codex"].diagnostics(banner) == ()


def test_extra_provider_fields_are_never_retained():
    error = json.loads(PANE.splitlines()[1][2:])
    error["private_debug"] = "SECRET_PROVIDER_PAYLOAD"
    signals = RUNTIMES["codex"].diagnostics("■ " + json.dumps(error))
    serialized = json.dumps([asdict(signal) for signal in signals])
    assert "SECRET_PROVIDER_PAYLOAD" not in serialized
    assert "ChatGPT account" not in serialized
    assert "message" not in serialized


def test_old_output_beyond_the_observation_window_is_not_scanned():
    assert RUNTIMES["codex"].diagnostics(PANE + "\nordinary output" * 80) == ()


def test_distinct_model_changes_survive_while_identical_metadata_coalesces():
    pane = (
        "• Model changed to example-a high\n"
        "• Model changed to example-a high\n"
        "• Model changed to example-b low\n›"
    )
    signals = RUNTIMES["codex"].diagnostics(pane)
    assert len(signals) == 2
    assert {signal.model for signal in signals} == {"example-a", "example-b"}
    assert len({signal.observation_key for signal in signals}) == 2


def test_existing_provider_failure_has_a_generic_metadata_signal():
    signals = RUNTIMES["codex"].diagnostics("■ Selected model is at capacity\n›")
    assert len(signals) == 1
    assert (signals[0].code, signals[0].reason, signals[0].model) == (
        "provider_failure",
        "provider",
        None,
    )
