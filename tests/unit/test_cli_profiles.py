"""Operator discovery never opens a provider or promotes a reservation."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone.cli import main
from agent_backbone.config import PROFILE_REGISTRY_PATH, agent_api_key
from agent_backbone.services.database import (
    ProfileIdentity,
    ProfileOperations,
    ProfileRegistry,
    ProfileReservation,
    check_profile_startup,
)


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


@pytest.mark.parametrize("pending", [False, True])
async def test_explicit_apply_recovers_hot_journal_and_replays_original_receipt(
    tmp_path, capsys, pending
):
    registry = ProfileRegistry.create(Path(PROFILE_REGISTRY_PATH).expanduser())
    request = json.loads(Path("docs/contracts/profiles-v1.json").read_text())["lifecycle"]["create"]
    operations = ProfileOperations(registry)
    expected = operations.begin(request)[0] if pending else operations.apply(request)
    with sqlite3.connect(registry.path) as conn:
        conn.execute("CREATE TABLE spill (data BLOB)")
        conn.execute("INSERT INTO spill VALUES (zeroblob(262144))")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sqlite3, sys; "
            "c = sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA cache_size=1'); "
            "c.execute('BEGIN IMMEDIATE'); "
            "c.execute(\"UPDATE lifecycle_operations SET receipt_json = 'uncommitted'\"); "
            "c.execute('UPDATE spill SET data=randomblob(262144)'); "
            "os._exit(0)",
            str(registry.path),
        ],
        check=True,
        timeout=10,
    )
    journal = Path(str(registry.path) + "-journal")
    before = registry.path.read_bytes(), journal.read_bytes()
    for args in (["list"], ["operation", request["request_id"]]):
        with pytest.raises(SystemExit) as result:
            main(["profiles", *args, "--json"])
        assert result.value.code == 1
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "registry_unavailable"
        assert (registry.path.read_bytes(), journal.read_bytes()) == before
    provider = tmp_path / "legacy"
    url = f"sqlite+aiosqlite:///{provider / 'backbone.db'}"
    with pytest.raises(ValueError, match="unavailable"):
        await check_profile_startup(url, provider)
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request))
    with pytest.raises(SystemExit) as result:
        main(["profiles", "apply", "--file", str(source), "--json"])
    assert result.value.code == (0 if pending else 1)
    assert json.loads(capsys.readouterr().out) == expected
    assert not journal.exists()
    assert operations.lookup(request["request_id"]) == expected
    await check_profile_startup(url, provider)
    assert not provider.exists()


@pytest.mark.parametrize("args", [["list"], ["show", "profile-a"], ["operation", "op-a"]])
def test_absent_registry_is_a_structured_json_error(args, capsys):
    fixtures = json.loads(Path("docs/contracts/profiles-v1.json").read_text())
    with pytest.raises(SystemExit) as result:
        main(["profiles", *args, "--json"])
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out) == fixtures["discovery"]["registry_absent"]
    assert not Path(PROFILE_REGISTRY_PATH).expanduser().parent.exists()


def test_broken_registry_is_a_different_error(capsys):
    path = Path(PROFILE_REGISTRY_PATH).expanduser()
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text("not a database")
    path.chmod(0o600)
    with pytest.raises(SystemExit) as result:
        main(["profiles", "list", "--json"])
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "registry_unavailable"
