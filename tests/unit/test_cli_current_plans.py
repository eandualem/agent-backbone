"""Current-plan CLI preserves retry payloads and full machine-readable results."""

import io
import json
from unittest.mock import AsyncMock, patch

import pytest

from tests.unit.api.routes.test_api_current_plans import publication
from tests.unit.test_cli_reports import isolated, run  # noqa: F401


def test_local_validation_and_exact_write_payload(monkeypatch, capsys):
    body = publication("a" * 32)
    receipt = {
        "receipt": {
            "author_id": "a" * 32,
            "plan_id": body["plan"]["plan_id"],
            "revision_id": "b" * 32,
            "updated_at": "2026-10-27T11:00:00Z",
        },
        "created": True,
    }
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(201, receipt))) as api:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(body)))
        assert run(["current-plan", "publish", "--file", "-", "--validate", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["valid"]
        api.assert_not_called()
        for _ in range(2):
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(body)))
            assert run(["current-plan", "publish", "--file", "-", "--json"]) == 0
            assert json.loads(capsys.readouterr().out) == receipt
            assert api.call_args.kwargs["json_body"] == body
        assert run(["current-plan", "publish", "--schema"]) == 0
        assert "base_revision_id" in json.loads(capsys.readouterr().out)["required"]


@pytest.mark.parametrize("width", [40, 80, 120])
def test_read_output_and_absent_state(monkeypatch, capsys, width):
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setenv("NO_COLOR", "1")
    body = publication("a" * 32)
    record = {
        "author_id": "a" * 32,
        "author_name": "ike",
        "agent_name": "ike",
        "plan_id": body["plan"]["plan_id"],
        "revision_id": "b" * 32,
        "updated_at": "2026-10-27T11:00:00Z",
        "age_seconds": 0,
        "stale": False,
        "plan": body["plan"],
    }
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, record))):
        assert run(["current-plan", "show", "a" * 32, "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == record
        assert run(["current-plan", "show", "a" * 32]) == 0
        output = capsys.readouterr().out
        assert "Current work" in output and "in_progress" in output
        assert "…" not in output
    page = {
        "items": [{"author_id": "a" * 32, "agent_name": "ike", "record": None}],
        "next_after_author_id": None,
        "generated_at": record["updated_at"],
        "stale_after_seconds": 86400,
    }
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, page))):
        assert run(["current-plan"]) == 0
        assert "Not published" in capsys.readouterr().out
