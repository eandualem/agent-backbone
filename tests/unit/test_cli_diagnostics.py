"""The investigator command reads bounded API metadata and never initializes storage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_backbone import cli
from agent_backbone.cli._common import read_client_config
from agent_backbone.cli.diagnostics import parse_since


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(["diagnostics", *argv])
    return int(exc.value.code or 0)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("BACKBONE_DATABASE_URL", raising=False)
    return data_dir


def _digest():
    return {
        "since": "2026-09-07T00:00:00.000000Z",
        "generated_at": "2026-09-08T00:00:00.000000Z",
        "groups": [
            {
                "category": "delivery",
                "code": "delivery_failed",
                "severity": "error",
                "agent_name": "app",
                "runtime": "shell",
                "first_seen_at": "2026-09-07T12:00:00.000000Z",
                "last_seen_at": "2026-09-07T12:05:00.000000Z",
                "occurrences": 3,
                "operation_count": 1,
                "sample_id": 8,
            }
        ],
        "total_groups": 2,
        "total_occurrences": 4,
        "has_more": True,
        "count_semantics": "retained occurrences for operation/code records last seen in interval",
        "coverage": {"earliest_retained_at": None, "retention_days": 30},
        "deliveries": {"attempts": 6, "outcomes": {"agent_working": 3, "delivered": 3}},
        "queue": {"pending": 1, "in_progress": 0, "oldest_pending_at": None},
    }


def test_digest_json_filters_and_no_direct_access(isolated, capsys):
    api = AsyncMock(return_value=(200, _digest()))
    with (
        patch("agent_backbone.cli._common.api", api),
        patch("agent_backbone.cli._common.Direct") as direct,
    ):
        assert _run(["--since", "2026-09-07T03:00:00+03:00", "--agent", "app", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _digest()
    path = api.call_args.args[2]
    assert urlsplit(path).path == "/api/diagnostics"
    assert parse_qs(urlsplit(path).query) == {
        "since": ["2026-09-07T00:00:00.000000Z"],
        "agent": ["app"],
        "limit": ["20"],
    }
    direct.assert_not_called()
    assert not isolated.exists()


def test_readable_digest_marks_count_semantics_and_truncation(capsys):
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, _digest()))):
        assert _run([]) == 0
    output = capsys.readouterr().out
    assert "delivery_failed" in output and "app" in output
    assert "show 8" in output
    assert "More groups exist" in output
    assert "6 attempt(s)" in output
    assert "include retained earlier repeats" in " ".join(output.split())


def test_show_uses_exact_record_endpoint(capsys):
    data = {"record": {"id": 8}, "operation_records": [], "has_more": False}
    api = AsyncMock(return_value=(200, data))
    with patch("agent_backbone.cli._common.api", api):
        assert _run(["show", "8", "--json"]) == 0
    assert api.call_args.args[2] == "/api/diagnostics/8"
    assert json.loads(capsys.readouterr().out) == data


def test_trace_uses_exact_operation_filter_and_exposes_truncation(capsys):
    record = {"id": 8, "operation_id": "op-a"}
    api = AsyncMock(return_value=(200, {"items": [record], "has_more": True, "next_before_id": 8}))
    with patch("agent_backbone.cli._common.api", api):
        assert _run(["trace", "op-a", "--json"]) == 0
    path = urlsplit(api.call_args.args[2])
    assert path.path == "/api/diagnostics/records"
    assert parse_qs(path.query) == {"operation_id": ["op-a"], "limit": ["100"]}
    assert json.loads(capsys.readouterr().out) == {
        "operation_id": "op-a",
        "count": 1,
        "truncated": True,
        "items": [record],
        "next_before_id": 8,
    }


def test_trace_text_prints_count_and_metadata(capsys):
    record = {
        "id": 8,
        "operation_id": "op-a",
        "code": "submission_unconfirmed",
        "severity": "error",
        "details": {"condition": "ready"},
    }
    response = (200, {"items": [record], "has_more": False, "next_before_id": None})
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=response)):
        assert _run(["trace", "op-a"]) == 0
    output = capsys.readouterr().out
    assert "1 record(s); truncated: no" in output
    assert "submission_unconfirmed" in output
    assert "condition:" in output and '"ready"' in output


@pytest.mark.parametrize(
    "response", [None, (200, {"items": [], "has_more": False, "next_before_id": None})]
)
def test_trace_missing_or_unavailable_is_nonzero(response, capsys):
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=response)):
        assert _run(["trace", "missing-op", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "diagnostics_unavailable"


@pytest.mark.parametrize(
    "response",
    [None, (503, {"detail": "PRIVATE_EXCEPTION_TEXT"}), (200, {"groups": "PRIVATE_BODY"})],
)
def test_unavailable_is_nonzero_structured_and_not_healthy(response, capsys, isolated):
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=response)):
        assert _run(["--json"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"] == "diagnostics_unavailable"
    assert "PRIVATE" not in output
    assert "groups" not in json.loads(output)
    assert not isolated.exists()


def test_missing_record_is_explicit(capsys):
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(404, {}))):
        assert _run(["show", "42"]) == 1
    assert "record 42 was not found" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--since", "2026-09-08T00:00:00"],
        ["--since", "0h"],
        ["--since", "-1h"],
        ["--since", "1000000000000000000000000d"],
        ["--limit", "0"],
        ["--limit", "101"],
        ["show", "0"],
        ["trace", ""],
        ["trace", "not an identifier"],
        ["trace", "op\ninvalid"],
        ["trace", "x" * 201],
    ],
)
def test_invalid_options_never_call_api(arguments):
    with patch("agent_backbone.cli._common.api", AsyncMock()) as api:
        assert _run(arguments) == 2
    api.assert_not_called()


def test_relative_since():
    before = datetime.now(UTC) - timedelta(hours=24)
    actual = datetime.fromisoformat(parse_since("24h").replace("Z", "+00:00"))
    after = datetime.now(UTC) - timedelta(hours=24)
    assert before <= actual <= after


async def test_client_reads_existing_address_without_schema_changes(isolated):
    isolated.mkdir()
    database = isolated / "backbone.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO settings VALUES (?, ?)",
            [
                ("backbone.host", '"localhost"'),
                ("backbone.port", "7444"),
                ("unrelated", '"PRIVATE_SETTINGS"'),
            ],
        )
    original = database.read_bytes()
    with patch("agent_backbone.cli._common.Direct") as direct:
        config = await read_client_config()
    assert config.backbone.host == "localhost"
    assert config.backbone.port == 7444
    direct.assert_not_called()
    assert database.read_bytes() == original
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("settings",)
        ]
