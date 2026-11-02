"""Validate agreed examples, without claiming their unimplemented operations work."""

import json
import re
from pathlib import Path

from pydantic import TypeAdapter

from agent_backbone.models import SetupRequest
from agent_backbone.services.runtimes import RUNTIMES

FIXTURES = json.loads((Path(__file__).parents[2] / "docs/contracts/profiles-v1.json").read_text())
IDENTITY = ("profile_id", "installation_id", "lifecycle_id")


def test_adoption_replay_and_repeat_preserve_the_original_tuple():
    adoption = FIXTURES["adoption"]
    original, repeated = adoption["request"], adoption["repeat_request"]
    assert original == repeated | {"request_id": original["request_id"]}
    assert original["request_id"] != repeated["request_id"]
    assert original["adoption_token"] == adoption["preview"]["adoption_token"]
    assert adoption["succeeded"]["installation_id"] == original["expected_installation_id"]
    assert adoption["repeat_succeeded"] == adoption["succeeded"] | {
        "request_id": repeated["request_id"]
    }
    for key in ("pending", "changed"):
        assert all(adoption[key][field] is None for field in IDENTITY)
    cases = {case["name"]: case for case in adoption["cases"]}
    assert cases["identical_body_replay"]["outcome"] == "succeeded"
    assert cases["new_request_existing_instance"]["outcome"] == "succeeded"
    assert cases["changed_body_same_id"]["outcome"] == "request_conflict"
    assert cases["stale_token_new_request"]["outcome"] == "adoption_changed"
    assert all(not case["allocates_identity"] for case in cases.values())
    for case in cases.values():
        assert case["outcome"] in {"succeeded", "request_conflict", "adoption_changed"}
        if receipt_name := case.get("expected_receipt"):
            receipt = adoption[receipt_name]
            assert receipt["request_id"] == adoption[case["request"]]["request_id"]
            assert case["outcome"] == (
                receipt["error"]["code"] if receipt["error"] else receipt["status"]
            )


def test_bound_setup_preserves_legacy_fields_and_cancellation_body():
    setup = FIXTURES["setup"]
    request = setup["first_agent"]
    assert setup["cancel_first_agent"] | {"action": "first_agent"} == request
    # Validate the unchanged fields with the existing schema, without enabling schema 2.
    legacy = {
        key: value
        for key, value in request.items()
        if key not in {"profile_id", "expected_installation_id", "expected_lifecycle_id"}
    } | {"schema_version": 1, "data_dir": "/example/provider"}
    assert TypeAdapter(SetupRequest).validate_python(legacy).model_dump() == legacy
    binding = {
        "profile_id": request["profile_id"],
        "installation_id": request["expected_installation_id"],
        "lifecycle_id": request["expected_lifecycle_id"],
    }
    for name in (
        "progress",
        "result",
        "failed_before_resolution",
        "cancellation_result",
        "cancellation_unsafe",
        "cancelled_original_result",
    ):
        event = setup[name]
        assert {key: event[key] for key in IDENTITY} == binding
        assert event["schema_version"] == 2
        assert event["request_id"] == request["request_id"]
        assert event["action"] == (
            "cancel_first_agent" if name.startswith("cancellation") else "first_agent"
        )
    assert setup["cancelled_original_result"]["code"] == "request_cancelled"
    assert setup["cancelled_original_result"]["status"] == "failed"
    assert setup["cancelled_original_result"]["retry"] == "new_request"
    assert setup["progress"]["seq"] < setup["result"]["seq"]
    assert setup["progress"]["type"] == "step"
    assert setup["failed_before_resolution"]["data"] == {}
    cancel = setup["cancellation_result"]
    assert cancel["retry"] == "new_request" and cancel["completed_steps"] == []
    assert cancel["data"] == {
        "cancelled_request_id": request["request_id"],
        "can_start_new_request": True,
    }


def test_bound_catalog_reuses_discovery_fields_without_equating_availability_with_login():
    catalog = FIXTURES["runtime_catalog"]
    for field in IDENTITY:
        assert catalog["catalog"][field] == FIXTURES["identity"][field]
    item = catalog["catalog"]["items"][0]
    assert item["authentication"] == "not_authenticated"
    assert item["available"] is True and item["setup_compatibility"]["compatible"] is True
    assert item["unattended"]["args"] == list(RUNTIMES[item["id"]].unattended_args)
    assert {case["authentication"] for case in catalog["authentication_cases"]} == {
        "not_authenticated",
        "unavailable",
        "unsupported",
    }


def test_permission_examples_keep_registration_and_unavailable_sources_explicit():
    context = FIXTURES["runtime_context"]
    registration = context["available"]["registration_id"]
    assert re.fullmatch(context["registration_pattern"], registration)
    for key in ("bound_list_member_fields", "bound_inspect_fields"):
        assert context[key] == {
            "registered": {"registration_id": registration},
            "unregistered": {"registration_id": None},
        }
    for name in ("available", "missing", "invalid", "unreadable", "unsupported"):
        response = context[name]
        assert response["registration_id"] == registration
        assert {key: response[key] for key in IDENTITY} == FIXTURES["socket_auth"]
        permissions = response["permissions"]
        for file in permissions["files"]:
            assert file["scope"] in {"runtime", "project", "project_local"}
            assert file["state"] in {"read", "missing", "invalid", "unreadable"}
            assert not Path(file["display_path"]).is_absolute()
            assert ".." not in Path(file["display_path"]).parts
        if name in {"missing", "invalid", "unreadable"}:
            assert permissions["status"] == "unavailable" and permissions["error"]
        for field in ("allow", "deny", "ask"):
            assert all(isinstance(value, str) for value in permissions[field])
        for mode in permissions["modes"]:
            assert set(mode) == {"name", "value"}
            assert all(isinstance(value, str) for value in mode.values())
    assert context["registration_changed"] == {
        "status": 409,
        "body": {
            "detail": {
                "code": "registration_changed",
                "message": "Agent registration does not match.",
            }
        },
    }
    for case in context["registration_cases"]:
        if case["outcome"] == "registration_changed":
            expected = case["query"]["expected_registration_id"]
            assert re.fullmatch(context["registration_pattern"], expected)
            assert expected != case["registered_id"]
        if case["registered_id"] is None and "expected_registration_id" in case["query"]:
            assert case["outcome"] == "registration_changed"
    assert context["redaction_case"]["returned_allow"] == []
