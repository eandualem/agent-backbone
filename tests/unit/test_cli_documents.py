"""Agent-usable publication and byte-preserving retrieval without local state."""

import json
from unittest.mock import AsyncMock, patch

from tests.unit.test_cli_reports import isolated, run  # noqa: F401


def test_publish_validate_retry_and_export(tmp_path, capsys, monkeypatch):
    source = tmp_path / "source.html"
    raw = b"<h1>exact</h1>\r\n<script>console.log('x')</script>"
    source.write_bytes(raw)
    args = ["documents", "publish", "--file", str(source), "--title", "Title", "--json"]
    with patch("agent_backbone.cli._common.api", AsyncMock()) as api:
        assert run(args + ["--validate"]) == 0
        assert json.loads(capsys.readouterr().out) == {"valid": True}
        api.assert_not_called()
    monkeypatch.setenv("BACKBONE_AGENT", "writer")
    monkeypatch.setenv("BACKBONE_API_KEY", "test-client-key")
    receipt = {"created": True, "record": {"id": 5}}
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(201, receipt))) as api:
        keys = []
        for extra in ([], [], ["--supersedes", "5"]):
            assert run(args + extra) == 0
            assert json.loads(capsys.readouterr().out) == receipt
            body = api.call_args.kwargs["json_body"]
            assert body["author"] == "writer" and body["html"].encode() == raw
            keys.append(body["request_id"])
        assert keys[0] == keys[1] != keys[2]
    target = tmp_path / "export.html"
    detail = {"id": 5, "html": raw.decode()}
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, detail))) as api:
        assert run(["documents", "show", "5", "--json", "--output", str(target)]) == 0
        assert json.loads(capsys.readouterr().out) == detail
        assert target.read_bytes() == raw
        assert api.call_args.args[2] == "/api/documents/5"
    page = {"items": [], "has_more": False, "next_before_id": None}
    with patch("agent_backbone.cli._common.api", AsyncMock(return_value=(200, page))) as api:
        assert run(["documents", "--author-id", "a" * 32, "--before", "7", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == page
        assert "author_id=" in api.call_args.args[2] and "before_id=7" in api.call_args.args[2]
