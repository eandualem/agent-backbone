"""Lifecycle identity/replay state lives outside replaceable provider storage."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_backbone.services.database import (
    ProfileIdentity,
    ProfileOperations,
    ProfileRegistry,
    ProfileReservation,
    validate_profile_operation,
)


@pytest.fixture
def operations(tmp_path):
    registry = ProfileRegistry.create(tmp_path / "registry" / "index.db")
    registry.reserve(
        ProfileReservation(
            ProfileIdentity("profile-a", "installation-a", "lifecycle-a1"),
            tmp_path / "provider-a",
            "Work",
            "baseline-example-v1",
        )
    )
    return ProfileOperations(registry)


@pytest.fixture
def fixtures():
    return json.loads(Path("docs/contracts/profiles-v1.json").read_text())


def test_pending_and_terminal_replay_never_repeat_or_overwrite(operations, fixtures):
    request = fixtures["lifecycle"]["start"]
    receipt, execute = operations.begin(request)
    assert execute
    assert receipt == fixtures["lifecycle"]["pending"]
    reopened = ProfileOperations(ProfileRegistry(operations.registry.path))
    assert reopened.begin(dict(reversed(list(request.items())))) == (receipt, False)
    assert reopened.lookup(request["request_id"]) == receipt
    changed = {**request, "action": "stop"}
    rejected, execute = reopened.begin(changed)
    assert not execute and rejected["error"]["code"] == "request_conflict"
    assert reopened.lookup(request["request_id"]) == receipt
    failed = operations.fail(request, "binding_mismatch")
    assert failed == fixtures["lifecycle"]["failed"]
    assert reopened.apply(request) == failed
    assert reopened.fail(request, "profile_not_ready") == failed


def test_opposing_actions_are_serialized_across_connections(operations, fixtures):
    first, second = fixtures["lifecycle"]["start"], fixtures["lifecycle"]["stop"]

    def begin(request):
        return ProfileOperations(ProfileRegistry(operations.registry.path)).begin(request)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(begin, (first, second)))
    assert sum(execute for _, execute in results) == 1
    assert sorted(r["status"] for r, _ in results) == ["failed", "pending"]
    busy = next(r for r, execute in results if not execute)
    assert busy["error"]["code"] == "operation_busy"
    # Independent profile work does not wait for A's unknown outcome.
    other, execute = operations.begin(
        {
            **first,
            "profile_id": "profile-b",
            "expected_installation_id": "installation-b",
            "request_id": "other",
        }
    )
    assert execute and other["profile_id"] == "profile-b"


def test_exact_concurrent_replay_is_reserved_once(operations, fixtures):
    request = fixtures["lifecycle"]["create"]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: operations.begin(request), range(4)))
    assert sum(execute for _, execute in results) == 1
    assert all(r == fixtures["lifecycle"]["create_pending"] for r, _ in results)


def test_unknown_lookup_is_read_only_and_matches_contract(operations, fixtures):
    before = operations.registry.path.read_bytes()
    expected = fixtures["operation_lookup_unknown"]["body"]
    assert operations.lookup(expected["request_id"]) == expected
    assert operations.registry.path.read_bytes() == before


def test_unready_and_stale_bindings_are_durable_refusals(operations, fixtures):
    request = fixtures["lifecycle"]["start"]
    assert operations.apply(request)["error"]["code"] == "profile_not_ready"
    stale = {**request, "request_id": "stale", "expected_lifecycle_id": "old"}
    assert operations.apply(stale)["error"]["code"] == "binding_mismatch"
    create = fixtures["lifecycle"]["create"]
    result = operations.apply(create)
    assert result["error"]["code"] == "baseline_unavailable"
    assert result["profile_id"] is None
    assert operations.lookup(create["request_id"]) == result
    assert not operations.registry.path.parent.parent.joinpath("provider-a").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"unexpected": "value"},
        {"request_id": "\n"},
        {"action": []},
        {"profile_id": None},
    ],
)
def test_invalid_input_is_rejected_before_reservation(operations, fixtures, change):
    before = operations.registry.path.read_bytes()
    with pytest.raises(ValueError):
        operations.begin({**fixtures["lifecycle"]["start"], **change})
    assert operations.registry.path.read_bytes() == before


def test_all_agreed_action_shapes_are_accepted(fixtures):
    for action in ("create", "start", "stop"):
        assert (
            validate_profile_operation(fixtures["lifecycle"][action])
            == fixtures["lifecycle"][action]
        )
    assert (
        validate_profile_operation(fixtures["adoption"]["request"])
        == fixtures["adoption"]["request"]
    )


@pytest.mark.parametrize("adoption_first", [False, True])
def test_adoption_serializes_with_lifecycle_on_its_installation(
    operations, fixtures, adoption_first
):
    start = fixtures["lifecycle"]["start"]
    adoption = {
        **fixtures["adoption"]["request"],
        "expected_installation_id": start["expected_installation_id"],
    }
    first, second = (adoption, start) if adoption_first else (start, adoption)
    assert operations.begin(first)[1]
    receipt, execute = operations.begin(second)
    assert not execute
    assert receipt["error"]["code"] == "operation_busy"
    assert operations.lookup(second["request_id"]) == receipt


def test_failed_reservation_is_an_invalidated_binding(operations, fixtures):
    request = fixtures["lifecycle"]["start"]
    operations.registry.invalidate(
        ProfileIdentity("profile-a", "installation-a", "lifecycle-a1"),
        operations.registry.path.parent.parent / "provider-a",
    )
    receipt = operations.apply(request)
    assert receipt["error"]["code"] == "binding_mismatch"
    assert operations.lookup(request["request_id"]) == receipt
