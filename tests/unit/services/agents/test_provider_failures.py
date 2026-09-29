"""Provider failures are visible, queue deliveries, and disappear after recovery."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.hooks import claude_hook, codex_hook
from agent_backbone.services.agents import (
    AgentState,
    get_agent_state,
    infer_state_from_pane,
    write_state_file,
)
from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.runtimes import RUNTIMES
from tests.unit.hooks.test_context import _opencode, _run

CASES = [
    ("codex", "Selected model is at capacity. Please try again later.", "›"),
    (
        "opencode",
        "You exceeded your current quota: generate_content_free_tier_requests, limit: 20",
        "Ask anything...",
    ),
    ("claude", "You've hit your limit · resets at 3 PM", "❯"),
]


@pytest.mark.parametrize("runtime,error,prompt", CASES)
def test_current_provider_banner_is_blocked(runtime, error, prompt):
    snapshot = infer_state_from_pane(
        f"\x1b[31m{error}\x1b[0m\nPlease retry in 49s\n{prompt}", runtime
    )
    assert snapshot.state == AgentState.BLOCKED and snapshot.reason == "provider"
    assert error in snapshot.detail and "49s" in snapshot.detail
    assert error in " ".join(snapshot.evidence)


@pytest.mark.parametrize("runtime,error,prompt", CASES)
def test_old_errors_and_quoted_examples_do_not_block(runtime, error, prompt):
    rt = RUNTIMES[runtime]
    banner = f"\x1b[31m{error}\x1b[0m"
    assert rt.provider_failure(f"{banner}\nCompleted the requested change.\n{prompt}") is None
    assert rt.provider_failure(f'Example: "{error}"\n{prompt}') is None
    assert rt.provider_failure(f"{banner}\nRunning tests now · esc to interrupt\n{prompt}") is None


@pytest.mark.parametrize(
    "runtime,error,prompt",
    CASES
    + [
        ("codex", "Error: Selected model is at capacity", "›"),
        ("codex", "You've hit your usage limit", "›"),
        ("codex", "Rate limit reached", "›"),
        ("codex", "Too many requests", "›"),
        ("codex", "insufficient_quota", "›"),
        ("claude", "API Error: 429", "❯"),
        ("claude", "Credit balance is too low", "❯"),
        ("claude", "Rate limit exceeded", "❯"),
        ("opencode", "Rate limit exceeded", "Ask anything..."),
        ("opencode", "RESOURCE_EXHAUSTED", "Ask anything..."),
        ("opencode", "Selected model is at capacity", "Ask anything..."),
    ],
)
async def test_unquoted_response_is_not_a_provider_banner(tmp_path, runtime, error, prompt):
    write_state_file(tmp_path, "app", {"state": "idle", "ts": time.time()})
    result = await get_agent_state(
        tmp_path, "app", runtime_hint=runtime, pane_content=f"{error}\n{prompt}"
    )
    assert result.state == AgentState.IDLE


@pytest.mark.parametrize("style", ["31", "91", "38;5;196", "38;2;240;80;80"])
def test_provider_error_foreground_formats(style):
    assert RUNTIMES["opencode"].provider_failure(f"\x1b[{style}m{CASES[1][1]}\x1b[0m")


@pytest.mark.parametrize("style", ["32", "48;5;196", "31;0", "38;2;50;200;200"])
def test_non_error_foregrounds_do_not_count(style):
    assert RUNTIMES["opencode"].provider_failure(f"\x1b[{style}m{CASES[1][1]}\x1b[0m") is None


@pytest.mark.parametrize("state,age", [("busy", 600), ("idle", 0)])
async def test_error_overrides_stale_busy_or_fresh_idle(tmp_path, state, age):
    write_state_file(
        tmp_path, "app", {"state": state, "ts": time.time() - age, "issue": 5, "repo": "acme/app"}
    )
    result = await get_agent_state(
        tmp_path, "app", runtime_hint="codex", pane_content="■ " + CASES[0][1] + "\n›"
    )
    assert result.state == AgentState.BLOCKED and result.reason == "provider"
    assert (result.current_issue, result.current_repo) == (5, "acme/app")
    recovered = await get_agent_state(
        tmp_path,
        "app",
        runtime_hint="codex",
        pane_content="■ " + CASES[0][1] + "\nCompleted successfully.\n›",
    )
    assert recovered.state == AgentState.IDLE


async def test_fresh_busy_hook_remains_authoritative(tmp_path):
    write_state_file(tmp_path, "app", {"state": "busy", "ts": time.time()})
    result = await get_agent_state(
        tmp_path, "app", runtime_hint="codex", pane_content="■ " + CASES[0][1] + "\n›"
    )
    assert result.state == AgentState.BUSY


async def test_provider_block_queues_even_priority_delivery(config, db):
    intelligence = "agent_backbone.services.routing._intelligence"
    with (
        patch(f"{intelligence}.list_sessions", AsyncMock(return_value=["ike"])),
        patch(f"{intelligence}.capture_pane", AsyncMock(return_value="■ " + CASES[0][1] + "\n›")),
        patch(f"{intelligence}.resolve_runtime", AsyncMock(return_value=RUNTIMES["codex"])),
        patch("agent_backbone.services.routing._delivery.send_message", AsyncMock()) as send,
    ):
        await safe_deliver(
            "ike",
            "new assignment",
            config,
            db=db,
            priority=True,
            delivery_kind="direct_message",
        )
    send.assert_not_awaited()
    assert await db.queue.pending_count("ike") == 1


def _deepcode_pane(status: str, reply: str = "", width: int = 80) -> str:
    """Deep Code 0.3.1 (live, local error endpoint)."""
    rule = "─" * width
    return (
        f" > say hi\n{reply}{status}\n{rule}\n>   Type your message...\n{rule}\n"
        "enter send · shift+enter newline · @ files · ctrl+v image · / commands · ctrl+d\n"
        "exit"
    )


@pytest.mark.parametrize(
    "error",
    [
        "HTTP 402: Insufficient Balance",
        "HTTP 429: Rate Limit Reached",
        "HTTP 503: Server Overloaded",
    ],
)
def test_deepcode_status_line_failure_is_blocked(error):
    reply = f" ✦ Request failed: {error} [type: invalid_request_error]\n"
    status = f"status: failed · deepseek-v4-flash max · fail: {error} [type: invalid_request_error]"
    # A tall pane leaves blank rows below Deep Code's footer.
    snapshot = infer_state_from_pane(_deepcode_pane(status, reply) + "\n" * 40, "deepcode")
    assert snapshot.state == AgentState.BLOCKED and snapshot.reason == "provider"
    assert snapshot.detail == f"{error} [type: invalid_request_error]"


@pytest.mark.parametrize(
    "width,status,detail",
    [
        (  # Deep Code wraps at spaces
            80,
            "status: failed · deepseek-v4-flash max · fail: HTTP 429: Rate Limit Reached\n"
            "[type: invalid_request_error, request ID: req-0123, trace ID:\n"
            "0123456789abcdef]",
            "HTTP 429: Rate Limit Reached [type: invalid_request_error, request ID: req-0123, "
            "trace ID: 0123456789abcdef]",
        ),
        (  # a narrower pane also splits words, even the status code's
            50,
            "status: failed · deepseek-v4-flash max · fail: HTT\nP 429: Rate Limit Reached",
            "HTTP 429: Rate Limit Reached",
        ),
        (
            64,
            "status: failed · deepseek-v4-flash max · fail: HTTP 429: Rate Li\n"
            "mit Reached\n"
            "[type: invalid_request_error, request ID: req-0123456789abcdef,\n"
            "trace ID:\n"
            "0123456789abcdef]",
            "HTTP 429: Rate Limit Reached [type: invalid_request_error, "
            "request ID: req-0123456789abcdef, trace ID: 0123456789abcdef]",
        ),
    ],
)
def test_deepcode_status_line_wrapped_in_a_narrow_pane(width, status, detail):
    pane = _deepcode_pane(status, width=width)
    assert RUNTIMES["deepcode"].provider_failure(pane) == detail


@pytest.mark.parametrize(
    "status,reply",
    [
        ("status: completed · deepseek-v4-flash max", " ✦ Request failed: HTTP 429: Rate Limit\n"),
        ("status: failed · deepseek-v4-flash max · fail: HTTP 401: Authentication Fails", ""),
        (
            "status: completed · deepseek-v4-flash max",
            " ✦ It said:\n   status: failed · deepseek-v4-flash max · fail: HTTP 429: Rate\n",
        ),
    ],
)
def test_deepcode_other_status_lines_do_not_block(status, reply):
    assert RUNTIMES["deepcode"].provider_failure(_deepcode_pane(status, reply)) is None


# Claude Code 2.1.283, captured live against a local Anthropic-compatible stub:
# the banner is "⏺" and text in one warning colour, a "✻ … · done" line follows,
# and blank lines pad the space above the input box at the bottom of the pane.
_CLAUDE_INPUT = (
    "\n" * 22
    + "\x1b[38;5;244m"
    + "─" * 60
    + "\n\x1b[39m❯\xa0\x1b[7m \x1b[0m\n"
    + "\x1b[38;5;244m"
    + "─" * 60
    + "\n"
    + "\x1b[39m  \x1b[38;5;246m⏸ manual mode on · ? for shortcuts · ← for agents\x1b[39m\n"
)


def _claude_turn(reply: str) -> str:
    done = "\x1b[38;5;246m✻\x1b[39m \x1b[38;5;246mChurned for 3m 6s · done 5:51 PM\x1b[39m"
    return f"❯ hello\n\n{reply}\n\n{done}\n{_CLAUDE_INPUT}"


def _warning(text: str) -> str:
    return f"\x1b[38;5;220m{text}\x1b[39m"


@pytest.mark.parametrize(
    "banner",
    [
        _warning("⏺")
        + " "
        + _warning("API Error: 529 Overloaded. This is a server-side issue")
        + "\n  "
        + _warning("usually temporary — try again in a moment."),
        _warning("⏺")
        + " "
        + _warning("API Error: Repeated 529 Overloaded errors. The API is busy"),
        _warning("⏺") + " " + _warning("API Error: Request rejected (429) · slow down"),
    ],
    ids=["529", "repeated-529", "429"],
)
def test_claude_2_1_283_provider_banner_is_blocked(banner):
    snapshot = infer_state_from_pane(_claude_turn(banner), "claude")
    assert snapshot.state == AgentState.BLOCKED and snapshot.reason == "provider"
    assert snapshot.detail.startswith("API Error: ")


def test_claude_earlier_provider_banner_is_blocked():
    banner = '  ⎿  API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'
    snapshot = infer_state_from_pane(f"❯ hello\n{banner}\n\n❯ \n", "claude")
    assert snapshot.state == AgentState.BLOCKED and snapshot.reason == "provider"


def test_claude_reply_quoting_a_provider_banner_is_not_blocked():
    reply = "\x1b[38;5;231m⏺\x1b[39m API Error: 529 Overloaded means the API is busy."
    assert infer_state_from_pane(_claude_turn(reply), "claude").state == AgentState.IDLE


def test_claude_reply_after_a_provider_banner_clears_it():
    banner = _warning("⏺") + " " + _warning("API Error: Repeated 529 Overloaded errors.")
    pane = _claude_turn(banner).replace(
        _CLAUDE_INPUT, "❯ hello again\n\n\x1b[38;5;231m⏺\x1b[39m stub reply\n" + _CLAUDE_INPUT
    )
    assert infer_state_from_pane(pane, "claude").state == AgentState.IDLE


# Live captures of a failed turn (#360): codex-cli 0.157.1 against a stub answering
# response.failed (server_is_overloaded), and OpenCode 1.18.32 against one answering
# 429 until it stopped retrying. Colours as drawn; the directory made neutral.
_CODEX_INPUT = (
    "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n"
    "  \x1b[38;2;246;226;183mstub-model default\x1b[39m · \x1b[38;2;171;223;167m/tmp/work\x1b[39m"
    "  ⚠ \x1b[38;2;196;167;103m5 warnings\x1b[39m · \x1b[1mf2\x1b[0m to view\n"
)


def _codex_turn(last: str) -> str:
    return f"\x1b[1;2m› \x1b[0mgo\n\n\n{last}\n\n\n{_CODEX_INPUT}"


_OC_BLUE, _OC_RED = "\x1b[38;2;92;156;245m", "\x1b[38;2;224;108;117m"
_OC_GREY, _OC_TEXT, _OC_WHITE = (
    "\x1b[38;2;128;128;128m",
    "\x1b[38;2;238;238;238m",
    "\x1b[38;2;255;255;255m",
)


def _opencode_block(bar: str, text: str, colour: str) -> str:
    return f"  {bar}┃\n  {bar}┃{_OC_WHITE}  {colour}{text}\n  {bar}┃\n\n"


def _opencode_turn(last: str) -> str:
    footer = f"     {_OC_BLUE}▣ {_OC_WHITE} {_OC_TEXT}Build{_OC_GREY} · Stub main"
    return (
        f"     {_OC_TEXT}stub reply\n\n{footer} · 9.3s\n\n"
        + _opencode_block(_OC_BLUE, "go", _OC_TEXT)
        + last
        + f"{footer}\n\n  {_OC_BLUE}┃\n"
        f"  {_OC_BLUE}┃{_OC_WHITE}  {_OC_BLUE}Build {_OC_GREY}· "
        f"{_OC_TEXT}Stub main {_OC_GREY}Local stub\n"
        f"  {_OC_BLUE}╹\x1b[38;2;30;30;30m{'▀' * 60}\n"
        f"   {_OC_GREY}/tmp/work {_OC_TEXT}ctrl+p {_OC_GREY}commands\n"
    )


FAILED = {
    "claude": _claude_turn(
        _warning("⏺") + " " + _warning("API Error: Repeated 529 Overloaded errors.")
    ),
    "codex": _codex_turn(
        "\x1b[38;5;1m■ Selected model is at capacity. Please try a different model.\x1b[39m"
    ),
    "opencode": _opencode_turn(_opencode_block(_OC_RED, "Rate limit exceeded", _OC_GREY)),
}
# The same words in a reply: the model's own output, not the CLI's banner.
ECHOED = {
    "claude": _claude_turn(
        "\x1b[38;5;231m⏺\x1b[39m API Error: 529 Overloaded means the API is busy."
    ),
    "codex": _codex_turn(
        "\x1b[2m• \x1b[0mThe stub answered:\n"
        "  ■ Selected model is at capacity. Please try a different model."
    ),
    "opencode": _opencode_turn(f"     {_OC_TEXT}Rate limit exceeded\n\n"),
}


@pytest.mark.parametrize("runtime", ["codex", "opencode"])
def test_the_current_failure_screen_is_blocked(runtime):
    snapshot = infer_state_from_pane(FAILED[runtime], runtime)
    assert snapshot.state == AgentState.BLOCKED and snapshot.reason == "provider"
    assert snapshot.detail.split(".")[0] in {"Selected model is at capacity", "Rate limit exceeded"}


@pytest.mark.parametrize(
    "last",
    [
        _opencode_block(_OC_BLUE, "Rate limit exceeded", _OC_TEXT),
        f"     {_OC_TEXT}Rate limit exceeded\n\n",
        _opencode_block(_OC_RED, "Rate limit exceeded", _OC_GREY)
        + f"     {_OC_TEXT}Done, it went through on retry.\n\n",
    ],
    ids=["own message", "reply", "reply after the failure"],
)
def test_opencode_output_without_a_red_bar_last_is_not_a_failure(last):
    assert RUNTIMES["opencode"].provider_failure(_opencode_turn(last)) is None


def _record(runtime, tmp_path, failed: bool) -> None:
    """A turn, as each CLI's hooks report it. When it fails at the provider,
    Claude Code runs StopFailure naming the error, OpenCode's plugin sees the
    session go idle and Codex runs no hook at all (live, #360)."""
    if runtime == "opencode":
        _opencode(tmp_path, ["busy", "idle"] if failed else ["busy"])
    else:
        hook = {"claude": claude_hook, "codex": codex_hook}[runtime]
        _run(hook, tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "s"})
        if failed and runtime == "claude":
            failure = {"error": "server_error", "last_assistant_message": "API Error: 529"}
            _run(hook, tmp_path, {"hook_event_name": "StopFailure", "session_id": "s", **failure})
    record = json.loads((tmp_path / "desk.json").read_text())
    record["ts"] -= 10  # read ten seconds later, well before the record goes stale
    (tmp_path / "desk.json").write_text(json.dumps(record))


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_a_failed_turn_reads_blocked_while_its_record_is_fresh(tmp_path, runtime):
    _record(runtime, tmp_path, failed=True)
    snapshot = await get_agent_state(
        tmp_path, "desk", runtime_hint=runtime, pane_content=FAILED[runtime]
    )
    assert snapshot.state == AgentState.BLOCKED, snapshot.evidence
    assert snapshot.reason == "provider"


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_a_reply_echoing_the_error_leaves_a_fresh_busy_record(tmp_path, runtime):
    _record(runtime, tmp_path, failed=False)
    snapshot = await get_agent_state(
        tmp_path, "desk", runtime_hint=runtime, pane_content=ECHOED[runtime]
    )
    assert snapshot.state == AgentState.BUSY, snapshot.evidence


async def test_a_failure_waits_for_a_newly_submitted_prompt_to_be_drawn(tmp_path):
    write_state_file(tmp_path, "desk", {"state": "busy", "ts": time.time() - 1})
    snapshot = await get_agent_state(
        tmp_path, "desk", runtime_hint="codex", pane_content=FAILED["codex"]
    )
    assert snapshot.state == AgentState.BUSY


async def test_a_failure_above_a_working_turn_leaves_the_busy_record(tmp_path):
    write_state_file(tmp_path, "desk", {"state": "busy", "ts": time.time() - 10})
    working = FAILED["codex"].replace(
        _CODEX_INPUT, "• Working (3s • esc to interrupt)\n\n" + _CODEX_INPUT
    )
    snapshot = await get_agent_state(tmp_path, "desk", runtime_hint="codex", pane_content=working)
    assert snapshot.state == AgentState.BUSY


async def test_a_turn_that_starts_while_the_pane_is_read_keeps_its_busy_record(tmp_path):
    write_state_file(tmp_path, "desk", {"state": "busy", "ts": time.time() - 10})

    async def new_turn_meanwhile(session):
        write_state_file(tmp_path, "desk", {"state": "busy", "ts": time.time()})
        return FAILED["codex"]

    with patch("agent_backbone.services.agents._inference.capture_pane", new_turn_meanwhile):
        snapshot = await get_agent_state(tmp_path, "desk", runtime_hint="codex")
    assert snapshot.state == AgentState.BUSY


def test_codex_input_is_never_output():
    # Only the input line and a pasted banner below it: nothing above is output.
    pane = "› explain\n■ Selected model is at capacity. Please try a different model.\n"
    assert RUNTIMES["codex"].provider_failure(pane) is None


async def test_a_record_written_while_the_pane_is_read_decides(tmp_path):
    write_state_file(tmp_path, "desk", {"state": "busy", "ts": time.time() - 10})

    async def dialog_meanwhile(session):
        record = {"state": "waiting_for_human", "reason": "permission", "ts": time.time()}
        write_state_file(tmp_path, "desk", record)
        return FAILED["codex"]

    with patch("agent_backbone.services.agents._inference.capture_pane", dialog_meanwhile):
        snapshot = await get_agent_state(tmp_path, "desk", runtime_hint="codex")
    assert snapshot.state == AgentState.WAITING_FOR_HUMAN
