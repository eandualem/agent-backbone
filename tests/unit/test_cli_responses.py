"""Response authoring and reads use the API without creating local agent state."""

import io
import json
from unittest.mock import AsyncMock, patch

from agent_backbone.models import AgentResponse
from tests.unit.test_cli_reports import isolated, run  # noqa: F401


def test_example_validation_and_publication_retry_key(monkeypatch, capsys):
    assert run(["respond", "--example"]) == 0
    example = json.loads(capsys.readouterr().out)
    AgentResponse.model_validate(example)
    monkeypatch.setenv("BACKBONE_AGENT", "writer")
    monkeypatch.setenv("BACKBONE_API_KEY", "test-client-key")
    receipt = {"created": True, "record": {"id": 9, "telegram_delivery": "pending"}}
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(201, receipt))) as api:
        keys = []
        for prior in (None, None, 8):
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(example)))
            args = ["respond", "--file", "-", "--json"]
            if prior:
                args += ["--supersedes", str(prior)]
            assert run(args) == 0
            assert json.loads(capsys.readouterr().out) == receipt
            payload = api.call_args.kwargs["json_body"]
            assert payload["agent"] == "writer" and payload["supersedes_id"] == prior
            keys.append(payload["request_id"])
        assert keys[0] == keys[1] != keys[2]


def test_validation_is_local_and_retrieval_keeps_complete_fields(tmp_path, capsys):
    path = tmp_path / "answer.json"
    path.write_text('{"question":"Ready?","answer":"Yes.","action":"No decision needed."}')
    with patch("agent_backbone.cli._common.api", AsyncMock()) as api:
        assert run(["respond", "--file", str(path), "--validate", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {"valid": True}
        api.assert_not_called()
    record = {
        "id": 9,
        "agent_name": "writer",
        "author_name": "writer",
        "created_at": "2026-01-01T00:00:00Z",
        "response": json.loads(path.read_text()),
        "supersedes_id": 8,
        "superseded_by": 10,
        "telegram_delivery": "sent",
        "telegram_audio_delivery": "pending",
    }
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, record))) as api:
        assert run(["responses", "show", "9"]) == 0
        assert api.call_args.args[2] == "/api/responses/9"
        out = capsys.readouterr().out
        for value in ("Ready?", "Yes.", "No decision needed.", "8", "10", "pending"):
            assert value in out
