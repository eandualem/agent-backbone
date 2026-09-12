"""Human views retain data at different widths without altering machine output."""

from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest
from rich.cells import cell_len
from rich.console import Console

from agent_backbone.cli import presentation
from agent_backbone.cli.agents import _print_inspection


def render(columns, rows, *, width, plain=True):
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(
        presentation.collection_view(
            "Example",
            columns,
            rows,
            width=width,
            plain=plain,
        )
    )
    return output.getvalue()


@pytest.mark.parametrize("width", [20, 40, 80, 120, 180])
def test_long_values_are_not_clipped_and_every_field_survives(width):
    columns = ("Skill", "Purpose", "Tags", "Agents", "State")
    out = render(
        columns,
        [
            (
                "backend",
                "word " * 100 + "END_SENTINEL",
                "python",
                "peer-one\npeer-two",
                "INVALID: missing file",
            )
        ],
        width=width,
    )
    assert all(cell_len(line) <= width for line in out.splitlines())
    # Normalize wrapping and table separators; the full long description survives.
    words = out.replace("|", " ").split()
    assert words.count("word") == 100
    assert "END_SENTINEL" in "".join(words)
    assert all(label in out for label in columns)
    assert "peer-one" in out and "peer-two" in out
    assert "INVALID" in out
    assert "…" not in out and "\x1b" not in out


@pytest.mark.parametrize("plain", [True, False])
def test_literal_markup_unicode_and_terminal_controls(plain):
    out = render(
        ("Name", "Purpose"),
        [("[bold]name[/bold]", "你好 é \x1b[2Jdanger\x07\x00\r\x9b")],
        width=80,
        plain=plain,
    )
    assert "[bold]name[/bold]" in out and "你好" in out and "danger" in out
    assert not any(c in out for c in ("\x1b", "\x00", "\x07", "\r", "\x9b"))
    assert all(cell_len(line) <= 80 for line in out.splitlines())
    assert ("+" if plain else "╭") in out


def test_empty_collection_has_explicit_message():
    assert "No entries." in render(("Name",), [], width=40)


@pytest.mark.parametrize("width", [80, 96, 120])
@pytest.mark.parametrize("plain", [True, False])
def test_usage_identifiers_do_not_push_last_column_off_screen(width, plain):
    columns = ("Agent / CLI", "Session", "Models", "Tokens", "Coverage")
    out = render(
        columns,
        [
            (
                "agent-backbone / codex",
                "37f2c1d897eea88eacff7ee4",
                "gpt-6-astra",
                "42,784,115",
                "measured",
            )
        ],
        width=width,
        plain=plain,
    )
    assert "Coverage" in out and "measured" in out
    assert "42,784,115" in out
    assert all(cell_len(line) <= width for line in out.splitlines())


def test_no_color_is_plain_even_on_a_terminal(monkeypatch):
    class Terminal(StringIO):
        def isatty(self):
            return True

    output = Terminal()
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setenv("NO_COLOR", "1")
    presentation.print_table("Things", ("Name", "State"), [("one", "valid")])
    assert "+" in output.getvalue() and "\x1b" not in output.getvalue()


def test_agent_inspection_retains_purpose_and_full_available_reply(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(presentation, "console", lambda: Console(file=output, width=80))
    _print_inspection(
        {
            "name": "research",
            "online": True,
            "known": True,
            "description": "Research with source evidence",
            "tags": ["research"],
            "state": "busy",
            "delivery": "agent_working",
            "last_message": "word " * 100 + "LAST_REPLY_END",
            "evidence": ["hook state busy"],
        }
    )
    out = output.getvalue()
    assert "Research with source evidence" in out
    assert "LAST_REPLY_END" in out and out.split().count("word") == 100
    assert "agent_working" in out and "hook state busy" in out


def test_inspect_json_keeps_payload_unformatted(monkeypatch, capsys, tmp_path):
    import json

    from agent_backbone import cli

    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "data"))
    payload = {
        "name": "peer",
        "state": "busy",
        "description": "[blue]literal",
        "tags": ["design"],
        "last_message": "x" * 500,
    }
    with (
        patch("agent_backbone.cli._common.api_up", AsyncMock(return_value=True)),
        patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, payload))),
        patch.object(presentation, "console", side_effect=AssertionError("JSON was rendered")),
        pytest.raises(SystemExit) as result,
    ):
        cli.main(["agent", "inspect", "peer", "--json"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == payload
