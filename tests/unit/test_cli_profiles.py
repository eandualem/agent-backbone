"""Operator discovery never opens a provider or promotes a reservation."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone.cli import main
from agent_backbone.config import PROFILE_REGISTRY_PATH, agent_api_key
from agent_backbone.services.database import ProfileIdentity, ProfileRegistry, ProfileReservation


def test_discovery_is_local_atomic_and_read_only(tmp_path, capsys):
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    record = ProfileReservation(
        ProfileIdentity("profile-a", "installation-a", "lifecycle-a1"),
        tmp_path / "provider",
        "Work",
        "baseline-example-v1",
    )
    registry.reserve(record)
    before = registry.path.read_bytes(), registry.path.stat().st_mtime_ns
    with patch("agent_backbone.cli.bootstrap_config", side_effect=AssertionError("provider read")):
        for args, expected in (
            (["list"], registry.list()),
            (["show", "profile-a"], registry.show("profile-a")),
        ):
            with pytest.raises(SystemExit) as result:
                main(["profiles", *args, "--json"])
            assert result.value.code == 0
            assert json.loads(capsys.readouterr().out) == expected
    assert before == (registry.path.read_bytes(), registry.path.stat().st_mtime_ns)
    assert not record.data_dir.exists()


@pytest.mark.parametrize("command", [["list"], ["show", "unknown"]])
def test_missing_registry_is_error_not_empty_success(command, capsys):
    path = Path(PROFILE_REGISTRY_PATH).expanduser()
    with pytest.raises(SystemExit) as result:
        main(["profiles", *command, "--json"])
    assert result.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unavailable" in captured.err
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
def test_every_runtime_agent_is_denied_before_registry_access(runtime, monkeypatch, capsys):
    monkeypatch.setenv("BACKBONE_RUNTIME", runtime)
    monkeypatch.setenv("BACKBONE_AGENT", "worker")
    with patch("agent_backbone.cli.profiles.ProfileRegistry") as registry:
        with pytest.raises(SystemExit) as result:
            main(["profiles", "list", "--json"])
    assert result.value.code == 1
    assert "local operator" in capsys.readouterr().err
    registry.assert_not_called()


@pytest.mark.parametrize(
    "key",
    [
        "BACKBONE_API_KEY",
        "BACKBONE_AGENT_CREDENTIAL",
        "BACKBONE_CLIENT_AGENT",
        "BACKBONE_CLIENT_REGISTRATION",
    ],
)
def test_agent_credential_alone_cannot_discover_profiles(key, monkeypatch):
    monkeypatch.setenv(key, agent_api_key("operator"))
    with patch("agent_backbone.cli.profiles.ProfileRegistry") as registry:
        with pytest.raises(SystemExit) as result:
            main(["profiles", "show", "profile-a", "--json"])
    assert result.value.code == 1
    registry.assert_not_called()


@pytest.mark.parametrize("width", [40, 80, 120])
def test_human_discovery_retains_fields(tmp_path, capsys, monkeypatch, width):
    monkeypatch.setenv("COLUMNS", str(width))
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    registry.reserve(
        ProfileReservation(
            ProfileIdentity("profile-a", "installation-a", "lifecycle-a1"),
            tmp_path / "provider",
            "Work [blue]",
            "baseline-v1",
        )
    )
    for args in (["list"], ["show", "profile-a"]):
        with pytest.raises(SystemExit) as result:
            main(["profiles", *args])
        assert result.value.code == 0
        output = capsys.readouterr().out
        for value in ("profile-a", "Work [blue]", "isolated", "creating"):
            assert value in output


async def test_lifecycle_refusal_replay_and_unknown_lookup(tmp_path, capsys):
    fixtures = json.loads(Path("docs/contracts/profiles-v1.json").read_text())
    request = fixtures["lifecycle"]["create"]
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request))
    receipts = []
    for args in (
        ["apply", "--file", str(source)],
        ["operation", request["request_id"]],
        ["apply", "--file", str(source)],
    ):
        with pytest.raises(SystemExit) as result:
            main(["profiles", *args, "--json"])
        assert result.value.code == 1
        receipts.append(json.loads(capsys.readouterr().out))
    assert receipts[0] == receipts[1] == receipts[2]
    assert receipts[0]["error"]["code"] == "baseline_unavailable"
    with pytest.raises(SystemExit) as result:
        main(["profiles", "operation", "operation-unknown", "--json"])
    assert result.value.code == fixtures["operation_lookup_unknown"]["exit_code"]
    assert json.loads(capsys.readouterr().out) == fixtures["operation_lookup_unknown"]["body"]


@pytest.mark.parametrize(
    "args",
    [
        ["apply", "--file", "/missing/request.json"],
        ["operation", "request"],
    ],
)
def test_managed_agent_cannot_apply_or_lookup(args, monkeypatch, capsys):
    monkeypatch.setenv("BACKBONE_AGENT", "worker")
    with patch("agent_backbone.cli.profiles.ProfileRegistry") as registry:
        with pytest.raises(SystemExit) as result:
            main(["profiles", *args, "--json"])
    assert result.value.code == 1
    assert "local operator" in capsys.readouterr().err
    registry.assert_not_called()


@pytest.mark.parametrize("body", ['{"action":"create","action":"stop"}', "[" * 2000])
def test_ambiguous_or_deep_json_is_rejected_before_registry_access(tmp_path, capsys, body):
    source = tmp_path / "request.json"
    source.write_text(body)
    with patch("agent_backbone.cli.profiles.ProfileRegistry") as registry:
        with pytest.raises(SystemExit) as result:
            main(["profiles", "apply", "--file", str(source), "--json"])
    assert result.value.code == 1
    assert capsys.readouterr().out == ""
    registry.assert_not_called()
