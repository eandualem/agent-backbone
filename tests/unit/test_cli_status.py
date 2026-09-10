"""Readable snapshots preserve evidence and keep terminal controls out of data."""

from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest
from rich.cells import cell_len
from rich.console import Console

from agent_backbone.cli import build_parser
from agent_backbone.cli.status import _status, snapshot, status_view
from agent_backbone.config import bootstrap_config


def render_status(data: dict, *, width: int = 100, color: bool = False, plain: bool = False) -> str:
    output = StringIO()
    console = Console(
        file=output,
        width=max(20, width),
        force_terminal=color,
        color_system="standard" if color and not plain else None,
        markup=False,
        highlight=False,
    )
    console.print(status_view(data, width=console.width, plain=plain))
    return output.getvalue().rstrip("\n")


def example():
    return {
        "api_online": True,
        "captured_at": 1000,
        "swarms": [],
        "agents": [
            {
                "name": "api",
                "runtime": "codex",
                "model": "example-model",
                "state": "busy",
                "online": True,
                "current_issue": 12,
                "current_repo": "acme/app",
                "last_message": "Checking imports.",
                "last_activity": 950,
                "tags": ["backend"],
            },
            {
                "name": "tests",
                "runtime": "claude",
                "state": "blocked",
                "reason": "provider",
                "detail": "Selected model is at capacity",
                "online": True,
                "tags": [],
            },
        ],
    }


@pytest.mark.parametrize("width", [40, 60, 80, 120])
def test_narrow_output_retains_state_and_has_no_long_lines(width):
    output = render_status(example(), width=width)
    assert all(cell_len(line) <= width for line in output.splitlines())
    assert "busy" in output and "blocked" in output
    assert ("app#12" if width < 100 else "acme/app#12") in output
    assert any("│" in line and "Agent" in line and "State" in line for line in output.splitlines())
    assert "\x1b" not in output
    assert "%" not in output


def test_remote_text_cannot_control_terminal():
    data = example()
    data["details"] = True
    data["agents"][0]["last_message"] = "\x1b[2J\r\nunsafe\x00\x07"
    output = render_status(data)
    assert "unsafe" in output
    assert not any(c in output for c in ("\x1b", "\r", "\x00", "\x07"))


def test_eighty_columns_keeps_ten_agents_in_single_aligned_rows():
    data = example()
    data["agents"] = [
        {
            "name": name,
            "state": "idle" if i == 9 else "offline",
            "online": i == 9,
            "runtime": "claude",
            "model": "a-very-long-model-name",
            "repo": "acme/app",
        }
        for i, name in enumerate([f"agent-{i}" for i in range(9)] + ["assistant-runtime"])
    ]
    output = render_status(data, width=80)
    rows = [line for line in output.splitlines() if line.startswith("│")]
    assert len(rows) == 11  # One header and one row per agent.
    assert [part.strip() for part in rows[0].split("│")[1:-1]] == [
        "Agent",
        "State",
        "CLI",
        "Model",
        "Tags",
        "Work",
    ]
    assert "assistant-runtime" in rows[1] and "idle" in rows[1]
    boundaries = [[i for i, char in enumerate(line) if char == "│"] for line in rows]
    assert all(positions == boundaries[0] for positions in boundaries)


def test_details_reveal_full_model_repo_and_last_reply():
    data = example()
    data["details"] = True
    output = render_status(data, width=80)
    assert "example-model" in output
    assert "acme/app#12" in output
    assert "Last reply: Checking imports." in output
    assert "Terminal activity 50s ago" in output


def test_literal_markup_and_wide_characters_do_not_break_columns():
    data = example()
    data["agents"][0]["name"] = "[red]界e\u0301"
    output = render_status(data, width=80)
    assert "[red]界e\u0301" in output
    assert all(cell_len(line) <= 80 for line in output.splitlines())


def test_plain_uses_ascii_borders_without_color():
    output = render_status(example(), width=80, plain=True, color=True)
    assert "+---" in output and "| Agent" in output
    assert "╭" not in output and "\x1b" not in output


@pytest.mark.parametrize("plain", [False, True])
async def test_watch_refresh_restores_screen_and_cursor_on_interrupt(monkeypatch, plain):
    class Terminal(StringIO):
        def isatty(self):
            return True

    terminal = Terminal()
    monkeypatch.setattr("sys.stdout", terminal)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    args = build_parser().parse_args(["status", "--watch"] + (["--plain"] if plain else []))
    with (
        patch("agent_backbone.cli.status.snapshot", AsyncMock(return_value=example())) as fetch,
        patch(
            "agent_backbone.cli.status.asyncio.sleep",
            AsyncMock(side_effect=[None, KeyboardInterrupt]),
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        await _status(args)
    output = terminal.getvalue()
    assert fetch.await_count == 2
    assert output.count("\x1b[?1049h") == 1
    assert output.count("\x1b[?1049l") == 1
    assert output.rfind("\x1b[?25h") > output.rfind("BACKBONE")
    assert "Ctrl-C to exit" in output


async def test_filters_and_json_keep_evidence(tmp_path):
    data = example()
    data["agents"][0]["evidence"] = ["hook state file api.json: busy"]
    args = build_parser().parse_args(["status", "--tag", "backend", "--json"])

    async def api(config, method, path, **kw):
        return (200, {"items": data["agents"] if path == "/api/agents" else []})

    with (
        patch(
            "agent_backbone.cli._common.read_client_config",
            AsyncMock(return_value=bootstrap_config(tmp_path)),
        ),
        patch("agent_backbone.cli._common.api", side_effect=api),
    ):
        result = await snapshot(args)
    assert [a["name"] for a in result["agents"]] == ["api"]
    assert result["agents"][0]["evidence"] == data["agents"][0]["evidence"]


async def test_auth_failure_is_not_reported_as_offline(tmp_path):
    with (
        patch(
            "agent_backbone.cli._common.read_client_config",
            AsyncMock(return_value=bootstrap_config(tmp_path)),
        ),
        patch("agent_backbone.cli._common.api", AsyncMock(return_value=(401, {}))),
        pytest.raises(ValueError, match="HTTP 401"),
    ):
        await snapshot(build_parser().parse_args(["status"]))


def test_tags_observed_model_and_directory_show_in_the_roster():
    data = example()
    data["agents"] = [
        {
            "name": "leo",
            "state": "idle",
            "online": True,
            "runtime": "claude",
            "model": "claude-opus-5",
            "model_source": "observed",
            "tags": ["writer", "research", "swarm:x"],
            "dir": "/Users/me/ws/leo",
        },
        {
            "name": "web",
            "state": "offline",
            "online": False,
            "runtime": "codex",
            "model": None,
            "model_source": "runtime_default",
            "tags": [],
            "dir": "/Users/me/ws/web",
        },
    ]
    output = render_status(data, width=110)
    leo = next(line for line in output.splitlines() if "│ leo" in line)
    assert "claude-opus-5" in leo and "research writer" in leo and "swarm:" not in leo
    web = next(line for line in output.splitlines() if "│ web" in line)
    cells = [part.strip() for part in web.split("│")[1:-1]]
    assert cells[3] == "default" and cells[4] == "" and cells[5] == "web"  # folder, not "—"
