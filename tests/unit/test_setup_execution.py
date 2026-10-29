"""Installer actions preserve data and reconcile interrupted writes without inference."""

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from agent_backbone.cli import build_parser, main, onboarding, service
from agent_backbone.services.database import SetupReceipt, SetupReceiptError


def payload(tmp_path, **changes):
    return {
        "schema_version": 1,
        "request_id": "initialize-1",
        "action": "initialize",
        "data_dir": str(tmp_path / "installation"),
        "service": "none",
        "starter_profile": "core-v1",
        **changes,
    }


def invoke(tmp_path, capsys, body, *, validate=False):
    source = tmp_path / "request.json"
    source.write_text(json.dumps(body))
    args = build_parser().parse_args(
        ["setup", "--file", str(source), "--jsonl"] + (["--validate"] if validate else [])
    )
    code = args.func(args)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "result"
    assert sum(e["type"] == "result" for e in events) == 1
    return code, events[-1]


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(onboarding, "_endpoint_occupied", AsyncMock(return_value=False))
    monkeypatch.setattr(onboarding._common, "api", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "setup_service_path", lambda: None)


def test_initialize_creates_editable_core_and_retry_preserves_user_edits(tmp_path, capsys, offline):
    body = payload(tmp_path)
    code, result = invoke(tmp_path, capsys, body)
    assert code == 0 and result["code"] == "initialized"
    data = Path(body["data_dir"])
    before = (data / ".env").read_bytes()
    assert b"BACKBONE_API_KEY=" in before
    policy = data / "templates/policies/starter-artifacts.md"
    policy.write_text("Owner's editable policy\n")
    code, again = invoke(tmp_path, capsys, body)
    assert code == 0 and again["code"] == "initialized"
    assert policy.read_text() == "Owner's editable policy\n"
    assert (data / ".env").read_bytes() == before
    assert result["completed_steps"] == ["preflight", "data", "starters"]
    assert before.decode().strip().split("=", 1)[1] not in json.dumps(result)
    assert (
        before.decode().strip().split("=", 1)[1]
        not in next((data / "setup-receipts").glob("*.json")).read_text()
    )


def test_existing_initialize_never_opens_migrating_database(tmp_path, capsys, offline, monkeypatch):
    body = payload(tmp_path)
    assert invoke(tmp_path, capsys, body)[0] == 0
    data = Path(body["data_dir"])
    before = {
        p.relative_to(data): p.read_bytes()
        for p in data.rglob("*")
        if p.is_file() and "setup-receipts" not in p.parts
    }
    monkeypatch.setattr(onboarding._common, "Direct", Mock(side_effect=AssertionError("migration")))
    code, result = invoke(tmp_path, capsys, body | {"request_id": "existing-2", "service": "user"})
    assert code == 0 and result["code"] == "existing_preserved"
    after = {
        p.relative_to(data): p.read_bytes()
        for p in data.rglob("*")
        if p.is_file() and "setup-receipts" not in p.parts
    }
    assert after == before


@pytest.mark.parametrize(
    "key,value", [("BACKBONE_AGENT_CREDENTIAL", ""), ("BACKBONE_AGENT", "guide")]
)
def test_managed_identity_cannot_read_operator_files(tmp_path, capsys, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        onboarding, "bootstrap_config", Mock(side_effect=AssertionError("secret read"))
    )
    code, result = invoke(tmp_path, capsys, payload(tmp_path))
    assert code == 3 and result["code"] == "operator_required"
    assert not (tmp_path / "installation").exists()


def test_validation_is_available_before_bootstrap(tmp_path, capsys, monkeypatch):
    import agent_backbone.cli as cli

    monkeypatch.setattr(cli, "bootstrap_config", Mock(side_effect=AssertionError("bootstrap")))
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        main(["setup", "--file", str(path), "--jsonl", "--validate"])
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["code"] == "validated"
    assert not (tmp_path / "installation").exists()


def test_interrupted_credential_publication_resumes_before_any_database_or_service(
    tmp_path, capsys, offline, monkeypatch
):
    body = payload(tmp_path)
    real = onboarding._exclusive_file
    monkeypatch.setattr(onboarding, "_exclusive_file", Mock(side_effect=OSError("interrupted")))
    assert invoke(tmp_path, capsys, body)[0] == 3
    (Path(body["data_dir"]) / ".setup-orphan").write_text("unpublished temporary bytes")
    monkeypatch.setattr(onboarding, "_exclusive_file", real)
    code, result = invoke(tmp_path, capsys, body)
    assert code == 0 and result["code"] == "initialized"
    assert (Path(body["data_dir"]) / ".env").is_file()


def test_interruption_after_credential_write_can_resume_own_file(
    tmp_path, capsys, offline, monkeypatch
):
    body = payload(tmp_path)
    real = onboarding._exclusive_file

    def interrupted(path, text):
        real(path, text)
        raise OSError("interrupted after write")

    monkeypatch.setattr(onboarding, "_exclusive_file", interrupted)
    assert invoke(tmp_path, capsys, body)[0] == 3
    secret = (Path(body["data_dir"]) / ".env").read_bytes()
    monkeypatch.setattr(onboarding, "_exclusive_file", real)
    assert invoke(tmp_path, capsys, body)[0] == 0
    assert (Path(body["data_dir"]) / ".env").read_bytes() == secret


def test_interrupted_credentials_changed_by_owner_are_preserved(
    tmp_path, capsys, offline, monkeypatch
):
    body = payload(tmp_path)
    real = onboarding._exclusive_file

    def interrupted(path, text):
        real(path, text)
        raise OSError("interrupted")

    monkeypatch.setattr(onboarding, "_exclusive_file", interrupted)
    assert invoke(tmp_path, capsys, body)[0] == 3
    env = Path(body["data_dir"]) / ".env"
    env.write_text("BACKBONE_API_KEY=owner-changed\n")
    monkeypatch.setattr(onboarding, "_exclusive_file", real)
    code, result = invoke(tmp_path, capsys, body)
    assert code == 3 and result["code"] == "installation_conflict"
    assert env.read_text() == "BACKBONE_API_KEY=owner-changed\n"


def test_receipts_bind_input_lock_and_retain_identity_at_capacity(tmp_path, monkeypatch):
    from agent_backbone.services.database import _setup_receipts as receipts

    body = payload(tmp_path)
    with SetupReceipt(tmp_path, body) as first:
        first.completed("data")
        with pytest.raises(SetupReceiptError, match="still running"):
            with SetupReceipt(tmp_path, body):
                pass
    with pytest.raises(SetupReceiptError) as exc:
        with SetupReceipt(tmp_path, body | {"service": "user"}):
            pass
    assert exc.value.code == "request_conflict"
    monkeypatch.setattr(receipts, "SETUP_RECEIPTS", 1)
    with SetupReceipt(tmp_path, body) as again:
        assert again.record["completed_steps"] == ["data"]
    with pytest.raises(SetupReceiptError, match="capacity"):
        with SetupReceipt(tmp_path, body | {"request_id": "another"}):
            pass


def test_receipt_symlink_cannot_read_or_overwrite_target(tmp_path):
    body = payload(tmp_path)
    receipts = tmp_path / "setup-receipts"
    receipts.mkdir(mode=0o700)
    target = tmp_path / "untouched"
    target.write_text("owner content")
    (receipts / (hashlib.sha256(body["request_id"].encode()).hexdigest() + ".json")).symlink_to(
        target
    )
    with pytest.raises(SetupReceiptError):
        with SetupReceipt(tmp_path, body):
            pass
    assert target.read_text() == "owner content"


def test_setup_binary_pins_interpreter_install_and_portable_manifest(tmp_path, monkeypatch):
    prefix = tmp_path / "venv"
    own = prefix / "bin/backbone"
    other = tmp_path / "bin/backbone"
    for path in (own, other):
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    monkeypatch.setattr(service.sys, "prefix", str(prefix))
    monkeypatch.setenv("PATH", str(other.parent))
    assert service.setup_binary() == own
    prefix.rename(tmp_path / "python")
    monkeypatch.setattr(service.sys, "prefix", str(tmp_path / "python"))
    (tmp_path / "engine-manifest.json").write_text(
        json.dumps({"schema_version": 1, "entrypoint": "bin/backbone"})
    )
    assert service.setup_binary() == other


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_service_definition_preserves_literal_paths_and_path_environment(
    tmp_path, monkeypatch, system
):
    import plistlib
    import shlex

    monkeypatch.setattr(service.platform, "system", lambda: system)
    directory = tmp_path / "data's %space\\name"
    binary = tmp_path / "engine's %space$HOME\\name/backbone"
    search = "/tools's %path\\bin:/usr/bin"
    monkeypatch.setenv("PATH", search)
    path, content = service.setup_service_definition(binary, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if system == "Darwin":
        doc = plistlib.loads(content.encode())
        assert doc["ProgramArguments"][0] == str(binary)
        assert doc["EnvironmentVariables"]["PATH"] == str(binary.parent) + ":" + search
    else:
        line = next(line for line in content.splitlines() if line.startswith("ExecStart="))
        # This form is one whole C-escaped quoted item, not shell quote concatenation.
        encoded = line.removeprefix("ExecStart=").removesuffix(" up")
        assert json.loads(encoded).replace("%%", "%").replace("$$", "$") == str(binary)
        pathline = next(
            line for line in content.splitlines() if line.startswith('Environment="PATH=')
        )
        assert (
            shlex.split(pathline.partition("=")[2])[0].replace("%%", "%")
            == "PATH=" + str(binary.parent) + ":" + search
        )
    assert service.setup_service_owner() == directory


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_service_start_never_stops_or_replaces_existing_session(tmp_path, monkeypatch, system):
    import subprocess

    monkeypatch.setattr(service.platform, "system", lambda: system)
    monkeypatch.setattr(service, "setup_service_state", lambda: "installed")
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 15
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(service.subprocess, "run", execute)
    monkeypatch.setattr(service, "loaded_setup_service_owner", lambda: None)
    assert service.start_setup_service(tmp_path / "data")
    assert not any(
        word in {"bootout", "restart", "kickstart", "stop"} for call in calls for word in call
    )


def test_atomic_publication_never_exposes_partial_destination(tmp_path, monkeypatch):
    destination = tmp_path / "credentials"
    real_link = onboarding.os.link

    def interrupted(source, target):
        assert Path(source).read_text() == "complete contents"
        assert not destination.exists()
        raise OSError("interrupted before publication")

    monkeypatch.setattr(onboarding.os, "link", interrupted)
    with pytest.raises(OSError):
        onboarding._exclusive_file(destination, "complete contents")
    assert not destination.exists()
    monkeypatch.setattr(onboarding.os, "link", real_link)
    onboarding._exclusive_file(destination, "complete contents")
    assert destination.read_text() == "complete contents"
    with pytest.raises(FileExistsError):
        onboarding._exclusive_file(destination, "replacement")
    assert destination.read_text() == "complete contents"


def test_fresh_setup_never_reuses_inherited_operator_key(tmp_path, capsys, offline, monkeypatch):
    monkeypatch.setenv("BACKBONE_API_KEY", "another-installation-key\nEXTRA=bad")
    code, result = invoke(tmp_path, capsys, payload(tmp_path))
    assert code == 0, result
    contents = (tmp_path / "installation/.env").read_text()
    assert "another-installation-key" not in contents and "EXTRA=" not in contents
    assert len(contents.splitlines()) == 1


def test_existing_operator_file_wins_over_unrelated_environment(
    tmp_path, capsys, offline, monkeypatch
):
    import asyncio

    body = payload(tmp_path)
    assert invoke(tmp_path, capsys, body)[0] == 0
    key = (Path(body["data_dir"]) / ".env").read_text().strip().split("=", 1)[1]
    monkeypatch.setenv("BACKBONE_API_KEY", "unrelated")
    config = asyncio.run(
        onboarding._common.read_client_config(body["data_dir"], strict=True, operator_file=True)
    )
    assert config.api_key == key
    (Path(body["data_dir"]) / ".env").write_text("UNRELATED=value\n")
    code, result = invoke(tmp_path, capsys, body | {"request_id": "inspect"})
    assert code == 3 and result["code"] == "operator_required"
    assert result["retry"] == "none"


def test_sqlalchemy_failure_still_emits_one_final_result(tmp_path, capsys, offline, monkeypatch):
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(
        onboarding._common,
        "Direct",
        Mock(side_effect=OperationalError("private SQL", {}, Exception("private details"))),
    )
    code, result = invoke(tmp_path, capsys, payload(tmp_path))
    assert code == 3 and result["code"] == "outcome_unknown"
    assert "private" not in str(result)


def test_invalid_service_launcher_fails_before_data_creation(
    tmp_path, capsys, offline, monkeypatch
):
    monkeypatch.setattr(service, "setup_binary", Mock(side_effect=ValueError("bad manifest")))
    code, result = invoke(tmp_path, capsys, payload(tmp_path, service="user"))
    assert code == 3 and result["code"] == "service_unavailable"
    assert not (tmp_path / "installation/.env").exists()
    assert not (tmp_path / "installation/backbone.db").exists()


def test_pending_health_retry_preserves_completed_assets_and_service(
    tmp_path, capsys, offline, monkeypatch
):
    body = payload(tmp_path, service="user")
    service_path = tmp_path / "setup.service"
    monkeypatch.setattr(service, "setup_service_path", lambda: service_path)
    monkeypatch.setattr(service, "setup_binary", lambda: tmp_path / "bin/backbone")
    start = Mock(return_value=True)
    monkeypatch.setattr(service, "start_setup_service", start)
    monkeypatch.setattr(onboarding, "_READINESS_SECONDS", 0.01)
    observed = {
        "data_dir": body["data_dir"],
        "installation": "created",
        "service_state": "running",
        "api_url": None,
        "health": "unknown",
    }
    monkeypatch.setattr(
        onboarding,
        "_observe",
        AsyncMock(side_effect=lambda *args, **kwargs: (dict(observed), "Not ready.")),
    )
    code, result = invoke(tmp_path, capsys, body)
    assert code == 3 and result["code"] == "service_unavailable"
    definition = service_path.read_bytes()
    policy = Path(body["data_dir"]) / "templates/policies/starter-authority.md"
    policy.write_text("Owner edit")
    monkeypatch.setenv("PATH", "/different/tools:/usr/bin")
    observed["health"] = "healthy"
    code, result = invoke(tmp_path, capsys, body)
    assert code == 0, result
    assert policy.read_text() == "Owner edit"
    assert service_path.read_bytes() == definition
    assert start.call_count == 1


async def test_cold_service_ready_after_old_window_succeeds_without_restart(config, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(onboarding, "monotonic", lambda: clock[0])

    async def advance(delay):
        clock[0] += delay

    async def observe(*args, **kwargs):
        data = onboarding._observation_data(config, "created")
        data["health"] = "healthy" if clock[0] >= 5.8 else "unknown"
        return data, "Not ready."

    monkeypatch.setattr(onboarding.asyncio, "sleep", advance)
    monkeypatch.setattr(onboarding, "_observe", observe)
    start = Mock(side_effect=AssertionError("observation must not restart services"))
    monkeypatch.setattr(service, "start_setup_service", start)
    observed, detail = await onboarding._wait_for_service(config)
    assert observed["health"] == "healthy" and detail == ""
    assert clock[0] == 6.0
    start.assert_not_called()


async def test_readiness_window_includes_probe_time_and_retains_reason(config, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(onboarding, "monotonic", lambda: clock[0])

    async def advance(delay):
        clock[0] += delay

    async def observe(*args, **kwargs):
        clock[0] += 4.0
        return onboarding._observation_data(config, "created"), "Identity authentication failed."

    monkeypatch.setattr(onboarding.asyncio, "sleep", advance)
    probe = AsyncMock(side_effect=observe)
    monkeypatch.setattr(onboarding, "_observe", probe)
    observed, detail = await onboarding._wait_for_service(config)
    assert observed["health"] == "unknown"
    assert "30 seconds" in detail and "Identity authentication failed" in detail
    assert probe.await_count == 7  # elapsed probe time consumes the window too


async def test_stalled_readiness_probe_is_cancelled_at_deadline(config, monkeypatch):
    import asyncio

    cancelled = []

    async def stall(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(onboarding, "_READINESS_SECONDS", 0.01)
    monkeypatch.setattr(onboarding, "_observe", stall)
    observed, detail = await onboarding._wait_for_service(config)
    assert observed["health"] == "unknown" and cancelled == [True]
    assert "observation did not finish" in detail


def test_readiness_deadline_also_bounds_executor_shutdown(config, monkeypatch, tmp_path):
    import asyncio
    import subprocess
    import time

    unit = tmp_path / "unit"
    unit.touch()
    monkeypatch.setattr(service, "setup_service_path", lambda: unit)
    monkeypatch.setattr(service, "setup_service_owner", lambda: config.data_dir.resolve())
    monkeypatch.setattr(onboarding, "_READINESS_SECONDS", 0.05)
    timeouts = []

    def slow_manager(command, **kwargs):
        timeout = kwargs["timeout"]
        timeouts.append(timeout)
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(service.subprocess, "run", slow_manager)
    monkeypatch.setattr(onboarding._common, "api", AsyncMock(return_value=None))
    started = time.monotonic()
    observed, _ = asyncio.run(onboarding._wait_for_service(config))
    # Includes default-executor shutdown, as cmd_setup does before emitting JSONL.
    assert time.monotonic() - started < 0.5
    assert timeouts and all(0 < timeout <= 0.05 for timeout in timeouts)
    assert observed["health"] == "unknown"


@pytest.mark.parametrize(
    "identity,health,expected",
    [
        (None, None, "identity endpoint did not respond"),
        ((401, {"detail": "private-secret"}), None, "authentication failed (HTTP 401)"),
        ((503, {"detail": "private-secret"}), None, "identity endpoint returned HTTP 503"),
        ((200, {"data_dir": "/private-secret"}), None, "selected data directory"),
        ("matched", None, "health endpoint did not respond"),
        ("matched", (503, {"detail": "private-secret"}), "health endpoint returned HTTP 503"),
        ("matched", (200, {"healthy": "private-secret"}), "invalid health result"),
        (
            "matched",
            (
                200,
                {
                    "healthy": False,
                    "components": {
                        "database": {"healthy": False, "error": "private-secret"},
                        "private-secret": {"healthy": False},
                    },
                },
            ),
            "Unhealthy components: database.",
        ),
    ],
)
async def test_setup_observation_diagnostics_are_actionable_and_redacted(
    config, monkeypatch, identity, health, expected
):
    monkeypatch.setattr(service, "setup_service_path", lambda: None)
    if identity == "matched":
        identity = (200, {"data_dir": str(config.data_dir)})
    request = AsyncMock(side_effect=[identity, health])
    monkeypatch.setattr(onboarding._common, "api", request)
    observed, detail = await onboarding._observe(config, "created")
    assert expected in detail and "private-secret" not in detail
    assert observed["health"] != "healthy"
    assert request.await_count == (
        2 if identity == (200, {"data_dir": str(config.data_dir)}) else 1
    )


async def test_setup_identity_matches_canonical_data_directory(tmp_path, monkeypatch):
    from agent_backbone.config import bootstrap_config

    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "selected"
    link.symlink_to(actual, target_is_directory=True)
    config = bootstrap_config(link)
    monkeypatch.setattr(service, "setup_service_path", lambda: None)
    monkeypatch.setattr(
        onboarding._common,
        "api",
        AsyncMock(
            side_effect=[
                (200, {"data_dir": str(actual)}),
                (200, {"healthy": True}),
            ]
        ),
    )
    observed, detail = await onboarding._observe(config, "created")
    assert observed["health"] == "healthy" and detail == ""


def test_setup_enable_failure_after_reload_reports_failing_command(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service, "setup_service_path", lambda: tmp_path / "unit")
    monkeypatch.setattr(service, "loaded_setup_service_owner", lambda: None)
    monkeypatch.setattr(service, "setup_service_state", lambda: "installed")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 7, b"private-secret", b"private-secret"),
        ]
    )
    monkeypatch.setattr(service.subprocess, "run", run)
    with pytest.raises(service.SetupServiceError) as error:
        service.start_setup_service(tmp_path / "data")
    assert "systemctl --user enable --now agent-backbone.service" in str(error.value)
    assert "exit 7" in str(error.value) and "private-secret" not in str(error.value)
    assert run.call_count == 2


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
@pytest.mark.parametrize("failure", ["exit", "missing", "timeout", "oserror"])
def test_setup_manager_errors_report_operation_without_raw_output(
    tmp_path, monkeypatch, system, failure
):
    import subprocess

    monkeypatch.setattr(service.platform, "system", lambda: system)
    monkeypatch.setattr(service, "setup_service_path", lambda: tmp_path / "private-secret")
    monkeypatch.setattr(service, "loaded_setup_service_owner", lambda: None)
    monkeypatch.setattr(service, "setup_service_state", lambda: "installed")
    responses = {
        "exit": subprocess.CompletedProcess([], 7, b"private-secret", b"private-secret"),
        "missing": FileNotFoundError("private-secret"),
        "timeout": subprocess.TimeoutExpired("private-secret", 15, stderr=b"private-secret"),
        "oserror": OSError("private-secret"),
    }
    response = responses[failure]
    run = (
        Mock(side_effect=response)
        if isinstance(response, Exception)
        else Mock(return_value=response)
    )
    monkeypatch.setattr(service.subprocess, "run", run)
    with pytest.raises(service.SetupServiceError) as error:
        service.start_setup_service(tmp_path / "data")
    assert "private-secret" not in str(error.value)
    assert ("systemctl" if system == "Linux" else "launchctl") in str(error.value)
    assert {
        "exit": "exit 7",
        "missing": "not available",
        "timeout": "15 seconds",
        "oserror": "could not be executed",
    }[failure] in str(error.value)
    assert run.call_count == 1


def test_setup_service_failure_keeps_retry_state_and_emits_diagnostics(
    tmp_path, capsys, offline, monkeypatch
):
    body = payload(tmp_path, service="user")
    monkeypatch.setattr(service, "setup_service_path", lambda: tmp_path / "unit")
    monkeypatch.setattr(service, "setup_binary", lambda: tmp_path / "bin/backbone")
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service, "setup_service_owner", lambda: None)
    monkeypatch.setattr(
        service,
        "start_setup_service",
        Mock(
            side_effect=service.SetupServiceError(
                "Service command `systemctl --user daemon-reload` failed (exit 1)."
            )
        ),
    )
    code, result = invoke(tmp_path, capsys, body)
    assert code == 3 and result["code"] == "service_unavailable"
    assert result["retry"] == "same_request"
    assert result["completed_steps"] == ["preflight", "data", "starters"]
    assert "exit 1" in result["message"] and "journalctl --user" in result["message"]
    data = Path(body["data_dir"])
    credential = (data / ".env").read_bytes()
    policy = data / "templates/policies/starter-authority.md"
    policy.write_text("Owner edit")
    start = service.start_setup_service
    start.side_effect = None
    start.return_value = True
    observed = onboarding._observation_data(onboarding.bootstrap_config(data), "created")
    observed["health"] = "healthy"
    monkeypatch.setattr(onboarding, "_observe", AsyncMock(return_value=(observed, "")))
    code, result = invoke(tmp_path, capsys, body)
    assert code == 0 and result["code"] == "initialized"
    assert start.call_count == 2
    assert (data / ".env").read_bytes() == credential
    assert policy.read_text() == "Owner edit"


@pytest.mark.parametrize(
    "body", [{"code": "agent_ready"}, {"code": "agent_ready", "status": "other"}]
)
def test_invalid_daemon_result_is_reported_without_traceback(
    tmp_path, capsys, monkeypatch, config, body
):
    from tests.unit.test_setup_models import FIRST_AGENT

    monkeypatch.setattr(onboarding._common, "read_client_config", AsyncMock(return_value=config))
    monkeypatch.setattr(onboarding._common, "api", AsyncMock(return_value=(200, body)))
    code, result = invoke(tmp_path, capsys, FIRST_AGENT | {"data_dir": str(tmp_path / "data")})
    assert code == 3 and result["code"] == "outcome_unknown"


def test_connect_refusal_is_known_not_sent(tmp_path, capsys, monkeypatch, config):
    from tests.unit.test_setup_models import FIRST_AGENT

    monkeypatch.setattr(onboarding._common, "read_client_config", AsyncMock(return_value=config))
    monkeypatch.setattr(onboarding._common, "api", AsyncMock(return_value=None))
    monkeypatch.setitem(onboarding._common._not_sent, "/api/setup/first-agent", True)
    code, result = invoke(tmp_path, capsys, FIRST_AGENT | {"data_dir": str(tmp_path / "data")})
    assert code == 3 and result["code"] == "service_unavailable"


def test_unrelated_directory_is_refused_without_writing_receipts(tmp_path, capsys, offline):
    directory = tmp_path / "installation"
    directory.mkdir()
    (directory / "owner-file").write_text("preserve")
    before = sorted(p.name for p in directory.iterdir())
    code, result = invoke(tmp_path, capsys, payload(tmp_path))
    assert code == 3 and result["code"] == "installation_conflict"
    assert sorted(p.name for p in directory.iterdir()) == before
    assert (directory / "owner-file").read_text() == "preserve"


def test_service_path_omits_relative_entries_without_blocking_install(tmp_path, monkeypatch):
    import plistlib

    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("PATH", ":.:~/bin:local/tools:/usr/bin:")
    binary = tmp_path / "bin/backbone"
    _, content = service.setup_service_definition(binary, tmp_path / "data")
    assert (
        plistlib.loads(content.encode())["EnvironmentVariables"]["PATH"]
        == str(binary.parent) + ":/usr/bin"
    )


def test_pinned_backbone_and_existing_tmux_precede_bundled_tmux_fallback(tmp_path, monkeypatch):
    import plistlib
    import shutil

    binary = tmp_path / "engine/bin/backbone"
    bundled = tmp_path / "engine/libexec/tmux"
    existing = tmp_path / "tools/tmux"
    for file in (binary, bundled, existing):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("#!/bin/sh\nexit 0\n")
        file.chmod(0o755)
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("PATH", str(existing.parent) + ":/usr/bin:/bin")
    _, content = service.setup_service_definition(binary, tmp_path / "data")
    search = plistlib.loads(content.encode())["EnvironmentVariables"]["PATH"]
    assert shutil.which("backbone", path=search) == str(binary)
    assert shutil.which("tmux", path=search) == str(existing)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    _, content = service.setup_service_definition(binary, tmp_path / "data")
    search = plistlib.loads(content.encode())["EnvironmentVariables"]["PATH"]
    assert shutil.which("backbone", path=search) == str(binary)
    assert shutil.which("tmux", path=search) == str(bundled)


@pytest.mark.parametrize(
    "contents", ["PROJECT_KEY=private\n", "BACKBONE_API_KEY=unrelated-project-key\n"]
)
def test_project_env_alone_never_causes_setup_receipt_writes(tmp_path, capsys, offline, contents):
    directory = tmp_path / "installation"
    directory.mkdir()
    env = directory / ".env"
    env.write_text(contents)
    code, result = invoke(tmp_path, capsys, payload(tmp_path))
    assert code == 3
    assert result["code"] in {"installation_conflict", "operator_required"}
    assert [p.name for p in directory.iterdir()] == [".env"]
    assert env.read_text() == contents


def test_environment_only_initialize_reports_same_operator_file_requirement(
    tmp_path, capsys, offline, monkeypatch
):
    body = payload(tmp_path)
    assert invoke(tmp_path, capsys, body)[0] == 0
    directory = Path(body["data_dir"])
    (directory / ".env").unlink()
    monkeypatch.setenv("BACKBONE_API_KEY", "environment-only-key")
    before = {
        str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()
    }
    code, result = invoke(tmp_path, capsys, body | {"request_id": "inspect-env-only"})
    assert code == 3 and result["code"] == "operator_required" and result["retry"] == "none"
    assert {
        str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize(
    "declared", ["", "BACKBONE_DATABASE_URL=postgresql+asyncpg://localhost/other\n"]
)
def test_inherited_external_database_does_not_establish_directory_ownership(
    tmp_path, capsys, offline, monkeypatch, declared
):
    body = payload(tmp_path)
    data = Path(body["data_dir"])
    data.mkdir()
    content = "BACKBONE_API_KEY=project-client-key\n" + declared
    (data / ".env").write_text(content)
    monkeypatch.setenv("BACKBONE_DATABASE_URL", "postgresql+asyncpg://localhost/backbone")
    reader = AsyncMock(side_effect=AssertionError("must not contact inherited database"))
    monkeypatch.setattr(onboarding._common, "read_client_config", reader)
    code, result = invoke(tmp_path, capsys, body)
    assert code == 3 and result["code"] == "installation_conflict"
    assert list(data.iterdir()) == [data / ".env"]
    assert (data / ".env").read_text() == content
    reader.assert_not_called()


async def test_explicit_external_database_still_uses_readonly_existing_flow(tmp_path, monkeypatch):
    data = tmp_path / "existing"
    data.mkdir()
    url = "postgresql+asyncpg://localhost/backbone"
    (data / ".env").write_text("BACKBONE_API_KEY=operator-key\nBACKBONE_DATABASE_URL=" + url + "\n")
    boot = onboarding.bootstrap_config(data)
    reader = AsyncMock(return_value=boot)
    monkeypatch.setattr(onboarding._common, "read_client_config", reader)
    assert await onboarding._inspect_existing(boot) is boot
    reader.assert_awaited_once_with(data, strict=True, operator_file=True)
    assert list(data.iterdir()) == [data / ".env"]


@pytest.mark.parametrize("finished", [False, True])
def test_fresh_retry_never_follows_an_inherited_external_database(
    tmp_path, capsys, offline, monkeypatch, finished
):
    body = payload(tmp_path)
    real = onboarding._exclusive_file
    if not finished:

        def interrupted(path, text):
            real(path, text)
            raise OSError("interrupted after publication")

        monkeypatch.setattr(onboarding, "_exclusive_file", interrupted)
    assert invoke(tmp_path, capsys, body)[0] == (0 if finished else 3)
    monkeypatch.setattr(onboarding, "_exclusive_file", real)
    external = tmp_path / "unrelated.db"
    external.write_bytes(b"unrelated existing bytes")
    monkeypatch.setenv("BACKBONE_DATABASE_URL", "sqlite+aiosqlite:///" + str(external))
    direct = Mock(side_effect=AssertionError("must not migrate external database"))
    monkeypatch.setattr(onboarding._common, "Direct", direct)
    code, result = invoke(tmp_path, capsys, body)
    assert code == 3 and result["code"] == "installation_conflict"
    assert external.read_bytes() == b"unrelated existing bytes"
    direct.assert_not_called()


@pytest.mark.parametrize(
    "state,owner", [("running", "foreign"), ("installed", "foreign"), ("running", None)]
)
def test_loaded_foreign_or_unknown_running_service_is_never_started(
    tmp_path, monkeypatch, state, owner
):
    monkeypatch.setattr(service, "setup_service_path", lambda: tmp_path / "service")
    monkeypatch.setattr(service, "setup_service_state", lambda: state)
    monkeypatch.setattr(
        service, "loaded_setup_service_owner", lambda: tmp_path / owner if owner else None
    )
    command = Mock(side_effect=AssertionError("must not reload or replace foreign service"))
    monkeypatch.setattr(service.subprocess, "run", command)
    assert service.start_setup_service(tmp_path / "selected") is False
    command.assert_not_called()


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_loaded_service_owner_comes_from_manager_not_definition(tmp_path, monkeypatch, system):
    directory = tmp_path / "loaded data"
    output = (
        f" environment = {{\n  BACKBONE_DATA_DIR => {directory}\n }}"
        if system == "Darwin"
        else f'"BACKBONE_DATA_DIR={directory}" PATH=/usr/bin'
    )
    monkeypatch.setattr(service.platform, "system", lambda: system)
    monkeypatch.setattr(
        service.subprocess, "run", Mock(return_value=Mock(returncode=0, stdout=output))
    )
    assert service.loaded_setup_service_owner() == directory
    monkeypatch.setattr(service, "setup_service_path", lambda: tmp_path / "new-definition")
    monkeypatch.setattr(service, "setup_service_state", lambda: "running")
    assert service.start_setup_service(directory) is True


def test_data_retry_preserves_port_written_before_interruption(
    tmp_path, capsys, offline, monkeypatch
):
    import sqlite3

    body = payload(tmp_path)
    original = SetupReceipt.completed

    def interrupted(self, step):
        if step == "data":
            raise OSError("lost after database settings commit")
        return original(self, step)

    monkeypatch.setattr(SetupReceipt, "completed", interrupted)
    assert invoke(tmp_path, capsys, body)[0] == 3
    database = Path(body["data_dir"]) / "backbone.db"
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE settings SET value=? WHERE key='backbone.port'", ("49001",))
    monkeypatch.setattr(SetupReceipt, "completed", original)
    assert invoke(tmp_path, capsys, body)[0] == 0
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute("SELECT value FROM settings WHERE key='backbone.port'").fetchone()[0]
            == "49001"
        )


def test_receipt_cleanup_runs_only_under_exclusive_lock(tmp_path):
    body = payload(tmp_path)
    data = Path(body["data_dir"])
    with SetupReceipt(data, body):
        temporary = data / "setup-receipts/.receipt-abandoned"
        temporary.write_text("incomplete")
        with pytest.raises(SetupReceiptError):
            with SetupReceipt(data, body):
                pass
        assert temporary.exists()
    with SetupReceipt(data, body) as receipt:
        assert not temporary.exists()
        assert receipt.path.exists()


def test_unsupported_hardlinks_have_actionable_result_without_partial_file(
    tmp_path, capsys, offline, monkeypatch
):
    import errno

    from agent_backbone import fs

    body = payload(tmp_path)
    monkeypatch.setattr(fs.os, "link", Mock(side_effect=OSError(errno.ENOTSUP, "unsupported")))
    code, result = invoke(tmp_path, capsys, body)
    assert code == 3 and result["code"] == "filesystem_unsupported"
    assert result["retry"] == "new_request"
    assert not (Path(body["data_dir"]) / ".env").exists()
    assert not list(Path(body["data_dir"]).glob(".setup-*"))


@pytest.mark.parametrize("response", ["cancelled", "lost", "conflict", "unsupported"])
def test_cancel_cli_uses_only_cancellation_endpoint(tmp_path, capsys, monkeypatch, response):
    from agent_backbone.models import setup_result

    body = {
        "schema_version": 1,
        "request_id": "cancel-original",
        "action": "cancel_first_agent",
        "data_dir": str(tmp_path / "data"),
        "hooks": ["codex"],
        "start_greeting": False,
        "agent": {
            "name": "guide",
            "directory": str(tmp_path / "project"),
            "create_directory": True,
            "runtime": "codex",
            "model": None,
            "brief": {"template": "orchestrator-v1"},
        },
    }
    monkeypatch.setattr(onboarding._common, "read_client_config", AsyncMock(return_value=object()))
    monkeypatch.setattr(onboarding._common, "request_not_sent", lambda path: False)
    if response == "cancelled":
        answer = (
            200,
            setup_result(
                "cancel_first_agent",
                "request_cancelled",
                "Cancelled.",
                status="completed",
                retry="new_request",
                data={"cancelled_request_id": body["request_id"], "can_start_new_request": True},
            ),
        )
    elif response == "conflict":
        answer = (200, setup_result("cancel_first_agent", "request_conflict", "Input differs."))
    elif response == "unsupported":
        answer = (404, {})
    else:
        answer = None
    api = AsyncMock(return_value=answer)
    monkeypatch.setattr(onboarding._common, "api", api)
    code, result = invoke(tmp_path, capsys, body)
    api.assert_awaited_once()
    assert api.await_args.args[1:3] == ("POST", "/api/setup/first-agent/cancel")
    assert api.await_args.kwargs["json_body"] == body
    assert result["action"] == "cancel_first_agent"
    assert code == (0 if response == "cancelled" else 3)
    if response != "cancelled":
        assert result["status"] != "completed"
    assert not (tmp_path / "data").exists()


def test_cancel_validation_cannot_execute(tmp_path, capsys, monkeypatch):
    from agent_backbone.models import SetupCancelFirstAgent

    body = {
        "schema_version": 1,
        "request_id": "cancel-original",
        "action": "cancel_first_agent",
        "data_dir": str(tmp_path / "data"),
        "hooks": ["codex"],
        "start_greeting": False,
        "agent": {
            "name": "guide",
            "directory": str(tmp_path / "project"),
            "create_directory": True,
            "runtime": "codex",
            "model": None,
            "brief": {"template": "orchestrator-v1"},
        },
    }
    assert SetupCancelFirstAgent.model_validate(body).original().action == "first_agent"
    monkeypatch.setattr(onboarding, "_execute", AsyncMock(side_effect=AssertionError("executed")))
    code, result = invoke(tmp_path, capsys, body, validate=True)
    assert code == 0 and result["code"] == "validated"
    assert not (tmp_path / "data").exists()


def test_cancel_cli_accepts_largest_original_request(tmp_path, capsys):
    from agent_backbone.models import SETUP_BODY_BYTES, SETUP_CANCEL_BODY_BYTES

    body = {
        "schema_version": 1,
        "request_id": "boundary",
        "action": "first_agent",
        "data_dir": str(tmp_path / "data"),
        "hooks": ["codex"],
        "start_greeting": False,
        "agent": {
            "name": "guide",
            "directory": "/workspace/guide",
            "create_directory": True,
            "runtime": "codex",
            "model": None,
            "brief": {"text": "x"},
        },
    }

    def encode():
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

    needed = SETUP_BODY_BYTES - len(encode())
    body["agent"]["brief"]["text"] += "界" * (needed // 3) + "x" * (needed % 3)
    source = tmp_path / "max-request.json"
    args = build_parser().parse_args(["setup", "--file", str(source), "--jsonl", "--validate"])
    assert len(encode()) == SETUP_BODY_BYTES
    source.write_bytes(encode())
    assert args.func(args) == 0
    body["action"] = "cancel_first_agent"
    assert len(encode()) == SETUP_CANCEL_BODY_BYTES
    source.write_bytes(encode())
    assert args.func(args) == 0
    source.write_bytes(encode() + b" ")
    assert args.func(args) == 2
    body["action"] = "first_agent"
    source.write_bytes(encode() + b" ")
    assert args.func(args) == 2
    assert not (tmp_path / "data").exists()
