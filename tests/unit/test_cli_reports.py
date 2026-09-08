"""Agent-facing authoring tools, useful errors and read-only browsing."""

import io
import json
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_backbone import cli
from agent_backbone.models import REPORT_BODY_BYTES, ProgressReport, report_example


def run(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    return exc.value.code or 0


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("BACKBONE_DATABASE_URL", raising=False)
    monkeypatch.delenv("BACKBONE_AGENT", raising=False)


def test_schema_and_example_are_available_without_service(capsys):
    with patch("agent_backbone.cli._common.api", AsyncMock()) as api:
        assert run(["report", "--example"]) == 0
        assert ProgressReport.model_validate_json(capsys.readouterr().out)
        assert run(["report", "--schema"]) == 0
        schema = json.loads(capsys.readouterr().out)
        assert "goal" in schema["required"]
        assert schema["x-report-limits"]["request_bytes"] == REPORT_BODY_BYTES
    api.assert_not_called()


def test_publish_from_stdin_uses_agent_identity_and_stable_retry_key(monkeypatch, capsys):
    monkeypatch.setenv("BACKBONE_AGENT", "writer")
    receipt = {"created": True, "record": {"id": 9, "agent_name": "writer"}}
    api = AsyncMock(return_value=(201, receipt))
    with patch("agent_backbone.cli._common.api", api):
        keys = []
        for _ in range(2):
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(report_example())))
            assert run(["report", "--file", "-", "--json"]) == 0
            assert json.loads(capsys.readouterr().out) == receipt
            payload = api.call_args.kwargs["json_body"]
            assert payload["agent"] == "writer"
            keys.append(payload["request_id"])
        assert keys[0] == keys[1]


def test_validate_and_oversize_rejection_never_contact_service(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report_example()))
    with patch("agent_backbone.cli._common.api", AsyncMock()) as api:
        assert run(["report", "--file", str(path), "--validate", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {"valid": True}
        data = report_example()
        data["progress"]["text"] = "PRIVATE_REJECTED" * 40
        path.write_text(json.dumps(data))
        assert run(["report", "--file", str(path), "--validate", "--json"]) == 2
        output = capsys.readouterr().out
        assert "progress.text" in output and "480" in output and "PRIVATE_REJECTED" not in output
        path.write_bytes(b"x" * (REPORT_BODY_BYTES + 1))
        assert run(["report", "--file", str(path), "--validate"]) == 2
        assert str(REPORT_BODY_BYTES) in capsys.readouterr().err
    api.assert_not_called()


def test_missing_identity_and_http_rejection_are_actionable(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report_example()))
    assert run(["report", "--file", str(path)]) == 2
    assert "--agent NAME" in capsys.readouterr().err
    with patch(
        "agent_backbone.cli._common.api",
        AsyncMock(return_value=(409, {"detail": "unchanged report"})),
    ):
        assert run(["report", "--file", str(path), "--agent", "writer"]) == 1
    assert "unchanged report" in capsys.readouterr().err


def test_updates_filters_json_and_no_database_initialization(tmp_path, capsys):
    page = {"items": [], "history": True, "has_more": False, "next_cursor": None}
    with (
        patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, page))) as api,
        patch("agent_backbone.cli._common.Direct") as direct,
    ):
        assert (
            run(
                [
                    "updates",
                    "--agent",
                    "a",
                    "--agent",
                    "b",
                    "--history",
                    "--cursor",
                    "token",
                    "--json",
                ]
            )
            == 0
        )
    assert json.loads(capsys.readouterr().out) == page
    assert parse_qs(urlsplit(api.call_args.args[2]).query) == {
        "agent": ["a", "b"],
        "history": ["true"],
        "members": ["false"],
        "limit": ["5"],
        "cursor": ["token"],
    }
    direct.assert_not_called()
    assert not (tmp_path / "data").exists()


def test_show_and_unavailable_service_are_explicit(capsys):
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, {"id": 8}))) as api:
        assert run(["updates", "show", "8", "--json"]) == 0
        assert api.call_args.args[2] == "/api/reports/8"
        assert json.loads(capsys.readouterr().out) == {"id": 8}
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=None)):
        assert run(["updates", "--json"]) == 1
    assert "unreachable" in capsys.readouterr().out


def test_summary_marks_old_missing_and_inactive_without_dumping_full_report(capsys):
    report = report_example()
    report["status"] = "inactive"
    record = {
        "id": 8,
        "agent_name": "writer",
        "author_name": "writer",
        "report": report,
        "age_seconds": 90000,
        "stale": True,
    }
    page = {
        "items": [
            {"agent_name": "writer", "record": record},
            {"agent_name": "new", "record": None},
        ],
        "history": False,
    }
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, page))):
        assert run(["updates"]) == 0
    output = capsys.readouterr().out
    assert "inactive" in output and "old report" in output and "new — no report yet" in output
    assert "backbone updates show ID" in output


@pytest.mark.parametrize(
    "args",
    [
        ["updates", "--limit", "21"],
        ["updates", "show", "-1"],
        ["updates", "--agent", "bad\nname"],
        ["report", "--example", "--validate"],
    ],
)
def test_invalid_arguments_do_not_call_api(args):
    with patch("agent_backbone.cli._common.api", AsyncMock()) as api:
        assert run(args) == 2
    api.assert_not_called()


def test_hostile_field_name_is_bounded_and_cannot_control_terminal(tmp_path, capsys):
    data = report_example()
    data["\x1b[2J" + "x" * 10000] = "extra"
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data))
    assert run(["report", "--file", str(path), "--validate"]) == 2
    error = capsys.readouterr().err
    assert len(error) < 500 and "\x1b" not in error
