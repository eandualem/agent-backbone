"""Bound discovery has one immutable transport and never falls back to local probes."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from agent_backbone.cli import _profile_catalog, build_parser
from agent_backbone.cli._profile_client import Binding, Refused

FIXTURES = json.loads((Path(__file__).parents[2] / "docs/contracts/profiles-v1.json").read_text())


class JsonStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield json.dumps(self.body).encode()


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "mismatch",
        "legacy",
        "invalid_version",
        "duplicate",
        "redirect",
        "unavailable",
        "unauthorized",
        "oversize",
    ],
)
async def test_catalog_pins_transport_and_checks_returned_identity(monkeypatch, case):
    calls = []
    binding = Binding.capture(
        FIXTURES["discovery"]["show_ready"],
        ("profile-a", "installation-a", "lifecycle-a1"),
        lambda _: "synthetic-credential",
    )
    body = copy.deepcopy(FIXTURES["runtime_catalog"]["catalog"])
    if case == "mismatch":
        body["lifecycle_id"] = "replacement"
    if case == "legacy":
        body = body["items"]
    if case == "invalid_version":
        body["schema_version"] = 2.0
    if case == "duplicate":
        body["items"].append(body["items"][0])
    if case == "oversize":
        body["padding"] = "x" * 65536

    async def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-credential"
        assert request.headers["X-Backbone-Lifecycle-Id"] == "lifecycle-a1"
        if request.url.path == "/api/profile":
            return httpx.Response(200, stream=JsonStream(FIXTURES["identity"]))
        assert (
            str(request.url)
            == "http://bound-provider/api/runtimes?schema_version=2&check_setup=true"
        )
        return httpx.Response(
            {"redirect": 302, "unavailable": 404, "unauthorized": 403}.get(case, 200),
            stream=JsonStream(body),
            headers={"Location": "http://unrelated.invalid"},
        )

    def transport(**kwargs):
        assert kwargs == {"uds": binding.endpoint, "retries": 0}
        return httpx.MockTransport(handle)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    if case == "valid":
        assert await _profile_catalog.catalog(binding) == body
    else:
        with pytest.raises(Refused):
            await _profile_catalog.catalog(binding)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "flags",
    [
        ["--profile", "profile-a"],
        ["--expected-installation-id", "installation-a"],
        [
            "--profile",
            "",
            "--expected-installation-id",
            "installation-a",
            "--expected-lifecycle-id",
            "lifecycle-a1",
        ],
    ],
)
def test_partial_binding_never_runs_legacy_discovery(flags, monkeypatch, capsys):
    from agent_backbone.services.runtimes import base

    monkeypatch.setattr(base.Runtime, "discovery", lambda *a, **kw: pytest.fail("local fallback"))
    args = build_parser().parse_args(["runtimes", "--json", "--check-setup", *flags])
    assert args.func(args) == 2
    assert capsys.readouterr().out == ""


def test_bound_cli_uses_existing_flags_and_operator_admission(monkeypatch, capsys):
    from agent_backbone.services.database import ProfileRegistry

    monkeypatch.setenv("BACKBONE_AGENT_CREDENTIAL", "")
    monkeypatch.setattr(ProfileRegistry, "show", lambda *a: pytest.fail("registry read"))
    args = build_parser().parse_args(FIXTURES["runtime_catalog"]["command"][1:])
    assert args.func(args) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "operator_required" in captured.err
