"""Provider start/stop act only on the verified registered instance."""

import signal
import socket
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_backbone.cli import _profile_services as module
from agent_backbone.cli._profile_client import Refused
from agent_backbone.cli._profile_services import ProviderServices
from agent_backbone.services.database import ProfileOperations, ProfileRegistry


@pytest.fixture
def created():
    # Pytest's tmp_path can exceed the Unix socket path limit on macOS.
    with tempfile.TemporaryDirectory(prefix="bb", dir="/tmp") as root:
        registry = ProfileRegistry.create(Path(root).resolve() / "registry.db")
        receipt = ProfileOperations(registry).apply(
            {
                "schema_version": 1,
                "request_id": "create-a",
                "action": "create",
                "display_name": "A",
                "baseline_version": "empty-v1",
            }
        )
        yield registry, receipt


def lifecycle(receipt, action="start", request_id="op-1", **changes):
    return {
        "schema_version": 1,
        "request_id": request_id,
        "action": action,
        "profile_id": receipt["profile_id"],
        "expected_installation_id": receipt["installation_id"],
        "expected_lifecycle_id": receipt["lifecycle_id"],
        **changes,
    }


def test_start_launches_nothing_when_the_identity_already_answers(created, monkeypatch):
    registry, receipt = created
    monkeypatch.setattr(module, "_answers", lambda binding: True)
    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    ProviderServices(registry).start(registry.ready_reservation(receipt["profile_id"]))
    popen.assert_not_called()


def test_start_launches_with_no_ambient_environment(created, monkeypatch):
    registry, receipt = created
    monkeypatch.setenv("GITHUB_TOKEN", "host-token")
    monkeypatch.setenv("PATH", "/usr/bin")
    answers = iter([False, True])
    monkeypatch.setattr(module, "_answers", lambda binding: next(answers))
    popen = MagicMock()
    popen.return_value.poll.return_value = None
    monkeypatch.setattr(subprocess, "Popen", popen)
    reservation = registry.ready_reservation(receipt["profile_id"])
    ProviderServices(registry).start(reservation)
    command, options = popen.call_args.args[0], popen.call_args.kwargs
    assert command[-4:] == ["serve", "--registry", str(registry.path), receipt["profile_id"]]
    assert options["env"].get("PATH") == "/usr/bin"
    assert set(options["env"]) <= {"PATH", "LANG", "LC_ALL"}
    assert options["cwd"] == reservation.data_dir / "home"
    assert options["start_new_session"] is True


def test_start_fails_when_the_provider_exits(created, monkeypatch):
    registry, receipt = created
    monkeypatch.setattr(module, "_answers", lambda binding: False)
    popen = MagicMock()
    popen.return_value.poll.return_value = 3
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(ValueError, match="did not start"):
        ProviderServices(registry).start(registry.ready_reservation(receipt["profile_id"]))


def test_stop_with_nothing_listening_signals_nobody(created, monkeypatch):
    registry, receipt = created
    kill = MagicMock()
    monkeypatch.setattr(module.os, "kill", kill)
    ProviderServices(registry).stop(registry.ready_reservation(receipt["profile_id"]))
    kill.assert_not_called()


@pytest.fixture
def listening(created):
    registry, receipt = created
    endpoint = registry.show(receipt["profile_id"])["binding"]["endpoint"]["path"]
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(endpoint)
        server.listen(4)
        yield registry, receipt, server


def test_stop_refuses_another_identity_without_signalling(listening, monkeypatch):
    registry, receipt, _ = listening

    async def mismatch(self):
        raise Refused("binding_mismatch")

    monkeypatch.setattr(module.Binding, "handshake", mismatch)
    kill = MagicMock()
    monkeypatch.setattr(module.os, "kill", kill)
    with pytest.raises(Refused):
        ProviderServices(registry).stop(registry.ready_reservation(receipt["profile_id"]))
    kill.assert_not_called()


def test_stop_signals_the_kernel_reported_process_and_waits(listening, monkeypatch):
    registry, receipt, server = listening

    async def verified(self):
        return {}

    monkeypatch.setattr(module.Binding, "handshake", verified)
    monkeypatch.setattr(module, "peer_pid", lambda connection: 4242)
    kills = []

    def kill(pid, sent):
        kills.append((pid, sent))
        server.close()

    monkeypatch.setattr(module.os, "kill", kill)
    ProviderServices(registry).stop(registry.ready_reservation(receipt["profile_id"]))
    assert kills == [(4242, signal.SIGTERM)]


def test_operations_run_the_exact_ready_tuple_once(created):
    registry, receipt = created
    services = MagicMock()
    operations = ProfileOperations(registry, services=services)
    started = operations.apply(lifecycle(receipt))
    assert (started["status"], started["error"]) == ("succeeded", None)
    assert operations.apply(lifecycle(receipt)) == started
    services.start.assert_called_once_with(registry.ready_reservation(receipt["profile_id"]))
    stopped = operations.apply(lifecycle(receipt, "stop", "op-2"))
    assert stopped["status"] == "succeeded"
    services.stop.assert_called_once()


def test_operations_refuse_a_stale_tuple_before_effects(created):
    registry, receipt = created
    services = MagicMock()
    result = ProfileOperations(registry, services=services).apply(
        lifecycle(receipt, expected_lifecycle_id="old")
    )
    assert result["error"]["code"] == "binding_mismatch"
    services.start.assert_not_called()


@pytest.mark.parametrize("action", ["start", "stop"])
def test_operations_report_a_service_failure_with_a_fixed_code(created, action):
    registry, receipt = created
    services = MagicMock()
    getattr(services, action).side_effect = ValueError("private detail")
    result = ProfileOperations(registry, services=services).apply(lifecycle(receipt, action))
    assert result["status"] == "failed"
    assert result["error"]["code"] == f"service_{action}_failed"
    assert "private" not in result["error"]["message"]


def test_without_services_a_ready_profile_stays_refused(created):
    registry, receipt = created
    result = ProfileOperations(registry).apply(lifecycle(receipt))
    assert result["error"]["code"] == "profile_not_ready"
