"""CLI validates before mutation and preserves exact edit request IDs."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from agent_backbone.cli import build_parser
from agent_backbone.cli.sheets import _export, cmd_sheets
from tests.conftest import make_config
from tests.sheet_support import tracker


async def test_stdout_export_preserves_bytes_and_prints_warnings(
    tmp_path, monkeypatch, capsysbinary
):
    raw = b"PK\x00\xff\r\n"
    warning = "View state is not retained."
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=raw, headers={"X-Backbone-Sheet-Warnings": json.dumps([warning])}
        )
    )
    client = httpx.AsyncClient
    monkeypatch.setattr(
        "agent_backbone.cli.sheets.httpx.AsyncClient",
        lambda **kwargs: client(transport=transport, **kwargs),
    )
    assert await _export(SimpleNamespace(output="-", json=False), make_config(tmp_path), "/") == 0
    captured = capsysbinary.readouterr()
    assert captured.out == raw
    assert warning.encode() in captured.err


def test_schema_validation_and_exact_payload(tmp_path, monkeypatch, capsys):
    payload = tracker().model_dump()
    path = tmp_path / "tracker.json"
    path.write_text(json.dumps(payload))
    args = build_parser().parse_args(
        ["sheets", "create", "--file", str(path), "--validate", "--json"]
    )
    assert cmd_sheets(args) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    api = AsyncMock(return_value=(201, {"record": {"sheet_id": "1" * 32}, "created": True}))
    monkeypatch.setattr("agent_backbone.cli._common.api", api)
    monkeypatch.setattr(
        "agent_backbone.cli._common.read_client_config", AsyncMock(return_value=object())
    )
    args.validate = False
    assert cmd_sheets(args) == 0
    assert api.call_args.kwargs["json_body"]["request_id"] == payload["request_id"]
    assert api.call_args.args[1:3] == ("POST", "/api/sheets")
    capsys.readouterr()
    assert cmd_sheets(build_parser().parse_args(["sheets", "create", "--schema"])) == 0
    assert "workbook" in json.loads(capsys.readouterr().out)["properties"]


def test_import_key_is_stable_and_no_operator_fallback(tmp_path, monkeypatch):
    path = tmp_path / "values.csv"
    path.write_text("name,value\nExample,=literal")
    api = AsyncMock(return_value=(403, {"detail": "signed author required"}))
    monkeypatch.setattr("agent_backbone.cli._common.api", api)
    monkeypatch.setattr(
        "agent_backbone.cli._common.read_client_config", AsyncMock(return_value=object())
    )
    args = build_parser().parse_args(
        [
            "sheets",
            "import",
            "--file",
            str(path),
            "--format",
            "csv",
            "--title",
            "Example",
            "--agent",
            "writer",
            "--json",
        ]
    )
    assert cmd_sheets(args) == 1
    first = api.call_args.kwargs["json_body"]
    assert cmd_sheets(args) == 1
    assert api.call_args.kwargs["json_body"] == first
    assert first["content"].endswith("=literal") and len(first["request_id"]) == 32
