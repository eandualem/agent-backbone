"""Same discovery contract for claude, codex, opencode, gemini, deepcode, aider and shell."""

import json
from unittest.mock import patch

import pytest

from agent_backbone import cli
from agent_backbone.services.runtimes import RUNTIMES


@pytest.mark.parametrize("runtime", RUNTIMES.values(), ids=RUNTIMES.keys())
@pytest.mark.parametrize("available", [False, True])
def test_discovery_never_treats_binary_presence_as_login(runtime, available):
    with (
        patch.object(type(runtime), "available", return_value=available),
        patch("subprocess.run", side_effect=AssertionError("no process probe")),
        patch("subprocess.Popen", side_effect=AssertionError("no process launch")),
    ):
        row = runtime.discovery()
    assert row["id"] == runtime.id
    assert row["available"] is available
    assert row["setup_supported"] is (runtime.id in {"claude", "codex", "opencode"})
    assert row["authentication"] == ("not_applicable" if runtime.id == "shell" else "unknown")
    assert row["models"] == list(runtime.models)
    assert row["efforts"] == list(runtime.efforts)
    supported = runtime.unattended_args is not None
    assert row["unattended"] == {
        "supported": supported,
        "args": list(runtime.unattended_args or ()),
        "sandboxed": runtime.sandboxed if supported else None,
    }


def test_cli_discovery_is_one_complete_json_object_without_storage(capsys, tmp_path, monkeypatch):
    data = tmp_path / "absent"
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(data))
    with (
        patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=False),
        patch("agent_backbone.cli.bootstrap_config", side_effect=AssertionError("no config")),
        pytest.raises(SystemExit) as result,
    ):
        cli.main(["runtimes", "--json"])
    assert result.value.code == 0
    output = capsys.readouterr()
    assert not output.err
    payload = json.loads(output.out)
    assert payload["schema_version"] == 1
    assert {row["id"] for row in payload["items"]} == set(RUNTIMES)
    assert not data.exists()


def test_discovery_works_with_an_invalid_inherited_credential(capsys, monkeypatch):
    monkeypatch.setenv("BACKBONE_AGENT_CREDENTIAL", "invalid")
    with (
        patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=False),
        pytest.raises(SystemExit) as result,
    ):
        cli.main(["runtimes", "--json"])
    assert result.value.code == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == len(RUNTIMES)


@pytest.mark.parametrize(
    "runtime", [rt for rt in RUNTIMES.values() if rt.binary], ids=lambda rt: rt.id
)
def test_discovery_requires_executable_fallback(runtime, tmp_path, monkeypatch):
    from agent_backbone.services.runtimes import base

    monkeypatch.setattr(base, "_FALLBACK_DIRS", (tmp_path,))
    monkeypatch.setattr(base.shutil, "which", lambda name: None)
    candidate = tmp_path / runtime.binary
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(0o600)
    assert runtime.discovery()["available"] is False
    candidate.chmod(0o700)
    assert runtime.discovery()["available"] is True


def test_nonexecutable_fallback_does_not_hide_later_executable(tmp_path, monkeypatch):
    from agent_backbone.services.runtimes import base

    first, second = tmp_path / "first", tmp_path / "second"
    for directory in (first, second):
        directory.mkdir()
        (directory / "tool").write_text("#!/bin/sh\nexit 0\n")
    (first / "tool").chmod(0o600)
    (second / "tool").chmod(0o700)
    monkeypatch.setattr(base, "_FALLBACK_DIRS", (first, second))
    monkeypatch.setattr(base.shutil, "which", lambda name: None)
    assert base.resolve_command("tool") == str(second / "tool")
