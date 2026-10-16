"""Usage command semantics and token-first presentation, without real services."""

import argparse
import json
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_backbone import cli
from agent_backbone.cli.usage import print_usage
from agent_backbone.services.agents import usage_view
from tests.conftest import make_config


def run(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    return exc.value.code or 0


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("BACKBONE_DATABASE_URL", raising=False)


def test_json_passes_filters_without_ansi_or_database(capsys):
    payload = {"items": [], "totals": {"total_tokens": 12345}}
    with (
        patch("agent_backbone.cli._common.api_up", AsyncMock(return_value=True)),
        patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, payload))) as api,
        patch("agent_backbone.cli._common.Direct") as direct,
    ):
        assert run(["usage", "--agent", "worker", "--current", "--runtime", "codex", "--json"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == payload and "\x1b" not in output
    query = parse_qs(urlsplit(api.call_args.args[2]).query)
    assert query["current_only"] == ["true"] and query["agent"] == ["worker"]
    assert query["runtime"] == ["codex"]
    direct.assert_not_called()


@pytest.mark.parametrize("width", [40, 80, 120])
async def test_empty_human_view_is_explicit(tmp_path, db, capsys, monkeypatch, width):
    monkeypatch.setenv("COLUMNS", str(width))
    data = await usage_view(make_config(tmp_path), db, refresh=False)
    print_usage(data, argparse.Namespace(view=None, by="session", cost=False))
    out = capsys.readouterr().out
    assert "No identified session usage yet" in out
    assert "unavailable" in out and "API estimate:" not in out
    assert "\x1b" not in out


def test_session_requires_id_before_network(capsys):
    with patch("agent_backbone.cli._common.api_up", AsyncMock()) as api:
        assert run(["usage", "session"]) == 2
    assert "requires a session ID" in capsys.readouterr().err
    api.assert_not_called()


def test_old_quick_start_still_accessible(capsys):
    assert run(["help", "usage"]) == 0
    assert "backbone agent start" in capsys.readouterr().out
    assert run(["help", "token-usage"]) == 0
    assert "Token usage" in capsys.readouterr().out
