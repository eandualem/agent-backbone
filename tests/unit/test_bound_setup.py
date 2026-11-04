"""Bound setup keeps client authority and receipt identity separate from legacy input."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from agent_backbone.models import BoundSetupRequest, SetupRequest
from agent_backbone.services.database import BoundSetupReceipt, SetupReceipt, SetupReceiptError

FIXTURE = json.loads((Path(__file__).parents[2] / "docs/contracts/profiles-v1.json").read_text())[
    "setup"
]
ADAPTER = TypeAdapter(BoundSetupRequest)


@pytest.mark.parametrize("action", ["first_agent", "cancel_first_agent"])
def test_canonical_bound_request_preserves_fields_and_rejects_legacy_authority(action):
    body = FIXTURE[action]
    assert ADAPTER.validate_python(body).model_dump() == body
    with pytest.raises(ValidationError):
        TypeAdapter(SetupRequest).validate_python(body)
    for extra in ("data_dir", "env", "credential", "native_root"):
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(body | {extra: "/other"})


def test_cancellation_normalizes_only_action_and_preserves_conflicts(tmp_path):
    first, cancel = FIXTURE["first_agent"], FIXTURE["cancel_first_agent"]
    assert ADAPTER.validate_python(cancel).original().model_dump() == first
    with BoundSetupReceipt(tmp_path, cancel) as receipt:
        receipt.cancel()
        path = receipt.path
    with BoundSetupReceipt(tmp_path, first) as receipt:
        assert receipt.cancelled and receipt.path == path
    with pytest.raises(SetupReceiptError, match="different input"):
        with BoundSetupReceipt(tmp_path, first | {"start_greeting": not first["start_greeting"]}):
            pytest.fail("changed input accepted")


@pytest.mark.parametrize(
    "field", ["profile_id", "expected_installation_id", "expected_lifecycle_id", "request_id"]
)
def test_full_tuple_receipt_namespaces_do_not_collide(tmp_path, field):
    body = FIXTURE["first_agent"]
    with BoundSetupReceipt(tmp_path, body) as first:
        path = first.path
    with BoundSetupReceipt(tmp_path, body | {field: "different"}) as other:
        assert other.path != path
    legacy = {
        key: value
        for key, value in body.items()
        if key not in {"profile_id", "expected_installation_id", "expected_lifecycle_id"}
    } | {"schema_version": 1, "data_dir": str(tmp_path)}
    with SetupReceipt(tmp_path, legacy) as other:
        assert other.path != path
