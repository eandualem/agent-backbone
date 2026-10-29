"""Transient setup input remains explicit and rejects ambiguous or unsafe shapes."""

import copy

import pytest
from pydantic import TypeAdapter, ValidationError

from agent_backbone.models import SetupRequest

ADAPTER = TypeAdapter(SetupRequest)
INITIALIZE = {
    "schema_version": 1,
    "request_id": "setup:initialize-1",
    "action": "initialize",
    "data_dir": "/workspace/data",
    "service": "none",
    "starter_profile": "core-v1",
}
FIRST_AGENT = {
    "schema_version": 1,
    "request_id": "setup:first-1",
    "action": "first_agent",
    "data_dir": "/workspace/data",
    "hooks": ["codex"],
    "agent": {
        "name": "guide",
        "directory": "/workspace/guide",
        "create_directory": True,
        "runtime": "codex",
        "model": None,
        "brief": {"template": "orchestrator-v1"},
    },
    "start_greeting": False,
}


@pytest.mark.parametrize(
    "payload", [INITIALIZE, FIRST_AGENT, FIRST_AGENT | {"action": "cancel_first_agent"}]
)
def test_exact_setup_contract_round_trips(payload):
    assert ADAPTER.validate_python(payload).model_dump() == payload


@pytest.mark.parametrize("path", ["relative", "~/data", "/tmp/../data", "/tmp/line\nbreak"])
def test_setup_rejects_implicit_or_ambiguous_paths(path):
    request = dict(INITIALIZE, data_dir=path)
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(request)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"request_id": ""},
        {"request_id": "with spaces"},
        {"start_greeting": "true"},
        {"credential": "not-accepted"},
        {"hooks": []},
        {"hooks": ["codex", "codex"]},
        {"hooks": ["claude"]},
    ],
)
def test_setup_refuses_implicit_consent_unknown_fields_and_hook_mismatch(change):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(FIRST_AGENT | change)


@pytest.mark.parametrize(
    "brief",
    [
        {"text": ""},
        {"text": "  "},
        {"template": "unknown"},
        {"template": "orchestrator-v1", "text": "conflict"},
    ],
)
def test_setup_brief_is_exactly_one_explicit_choice(brief):
    request = copy.deepcopy(FIRST_AGENT)
    request["agent"]["brief"] = brief
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(request)


def test_cancellation_reconstructs_the_exact_original_input():
    cancel = ADAPTER.validate_python(FIRST_AGENT | {"action": "cancel_first_agent"})
    assert cancel.original().model_dump() == FIRST_AGENT
