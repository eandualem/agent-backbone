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


# Claude Code 2.1.283, captured live against a local Anthropic-compatible stub.
def _claude_banner(text: str) -> str:
    return f"\x1b[38;5;220m\x1b[49m⏺\x1b[39m \x1b[38;5;220m{text}\x1b[39m"


def _claude_model_line(text: str) -> str:
    return f"\x1b[38;5;246m\x1b[49m  ⎿  \x1b[39m{text}"


_ACCENT = "\x1b[38;5;153m{}\x1b[39m"


@pytest.mark.parametrize(
    ("banner", "status"),
    [
        (_claude_banner("API Error: 400 stub bad request"), 400),
        (_claude_banner("API Error: Request rejected (429) · stub slow down"), 429),
        (
            _claude_banner(
                "API Error: 500 Internal server error. This is a server-side issue, usually "
                "temporary — try again in a moment. "
            )
            + "\n  \x1b[38;5;220mIf it persists, check your inference gateway.\x1b[39m",
            500,
        ),
        (_claude_banner("API Error: Repeated 529 Overloaded errors. The API is at capacity"), 529),
    ],
)
def test_claude_records_a_request_error_status_without_its_message(banner, status):
    signals = RUNTIMES["claude"].diagnostics(banner + "\n❯ \n")
    errors = [signal for signal in signals if signal.code == "request_error"]
    assert len(errors) == 1 and errors[0].http_status == status
    serialized = json.dumps([asdict(signal) for signal in signals])
    assert "stub" not in serialized and "server-side" not in serialized


def test_claude_records_an_unavailable_model_when_the_banner_wraps():
    model = "claude-opus-5-5[1m]"
    wide = (
        _claude_banner(f"There's an issue with the selected model ({model}). It may not exist")
        + "\n  \x1b[38;5;220mit. Run /model to pick a different model.\x1b[39m"
    )
    narrow = (
        _claude_banner("There's an issue with the selected model")
        + f"\n  \x1b[38;5;220m({model}). It may not exist\x1b[39m"
    )
    for pane in (wide, narrow):
        (signal,) = RUNTIMES["claude"].diagnostics(pane + "\n❯ \n")
        assert (signal.code, signal.reason, signal.model) == (
            "request_error",
            "model_unavailable",
            model,
        )


def test_claude_banner_colour_carries_onto_a_wrapped_line():
    # tmux does not repeat a colour that continues onto the next row.
    pane = "\x1b[38;5;220m⏺ API Error: Request rejected\n  (429) · slow down\x1b[39m\n❯ \n"
    (signal,) = RUNTIMES["claude"].diagnostics(pane)
    assert (signal.code, signal.http_status) == ("request_error", 429)


@pytest.mark.parametrize(
    ("line", "model", "effort"),
    [
        (
            "Set model to " + _ACCENT.format("Sonnet 5") + " and saved as your default for "
            "new sessions",
            "Sonnet 5",
            None,
        ),
        (
            "Set model to " + _ACCENT.format("Opus 5.5 (1M context) (default)") + " and saved "
            "as your default for new sessions",
            "Opus 5.5 (1M context) (default)",
            None,
        ),
        (
            "Set model to "
            + _ACCENT.format("Sonnet 5")
            + " for this session only with "
            + _ACCENT.format("xhigh")
            + " effort",
            "Sonnet 5",
            "xhigh",
        ),
        (
            "Set model to " + _ACCENT.format("Sonnet 5") + " and saved as your default for "
            "new sessions with " + _ACCENT.format("high") + " effort",
            "Sonnet 5",
            "high",
        ),
    ],
)
def test_claude_records_a_model_change_with_the_effort_shown(line, model, effort):
    (signal,) = RUNTIMES["claude"].diagnostics(_claude_model_line(line) + "\n❯ \n")
    assert (signal.code, signal.severity, signal.model, signal.observed_effort) == (
        "model_changed",
        "info",
        model,
        effort,
    )


def test_claude_records_a_model_change_wrapped_by_a_narrow_pane():
    suffix_wrapped = (
        _claude_model_line(
            "Set model to " + _ACCENT.format("Opus 5.5 (1M context) (default)") + " and saved as"
        )
        + "\n     your default for new sessions"
    )
    effort_wrapped = (
        _claude_model_line(
            "Set model to " + _ACCENT.format("Sonnet 5") + " for this session only with"
        )
        + "\n     "
        + _ACCENT.format("xhigh")
        + " effort"
    )
    # Claude Code briefly shows the new effort right-aligned below the change.
    indicator = "\n" + " " * 42 + "● high · /effort"
    pane = suffix_wrapped + indicator + "\n" + effort_wrapped + indicator + "\n❯ \n"
    signals = RUNTIMES["claude"].diagnostics(pane)
    assert [(signal.model, signal.observed_effort) for signal in signals] == [
        ("Opus 5.5 (1M context) (default)", None),
        ("Sonnet 5", "xhigh"),
    ]


@pytest.mark.parametrize(
    "pane",
    [
        # A reply quoting a banner: white glyph, text in the default colour.
        "\x1b[38;5;231m\x1b[49m⏺\x1b[39m API Error: 500 Internal server error",
        # Only part of the banner's text in the glyph's colour.
        "\x1b[38;5;220m⏺\x1b[39m \x1b[38;5;220mAPI \x1b[39mError: 400 stub bad request",
        "  ⎿  Set model to Sonnet 5 and saved as your default for new sessions",
        # Claude Code is still retrying; the final banner is the observation.
        "\x1b[38;5;211m\x1b[49m✻\x1b[39m \x1b[38;5;211m500 Internal server error\x1b[38;5;246m"
        " · Retrying in 2s · attempt 3/10\x1b[39m",
    ],
    ids=["quoted-banner", "partly-coloured-banner", "uncoloured-model-line", "retry-line"],
)
def test_claude_ignores_quoted_banners_and_retries(pane):
    assert RUNTIMES["claude"].diagnostics(pane + "\n❯ \n") == ()
