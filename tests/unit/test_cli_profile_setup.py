import asyncio
import copy
import json
import tempfile
from pathlib import Path

import pytest

from agent_backbone.cli._profile_client import Binding, Refused
from agent_backbone.cli._profile_setup import PendingSetup


@pytest.fixture
def fixtures():
    root = Path(__file__).resolve().parents[2]
    return json.loads((root / "docs/contracts/profiles-v1.json").read_text())


@pytest.fixture
def binding(fixtures):
    return Binding.capture(
        fixtures["discovery"]["show_ready"],
        ("profile-a", "installation-a", "lifecycle-a1"),
        lambda _: "synthetic-credential",
    )


def test_capture_and_cancel_preserve_original_values_and_connection(fixtures, binding):
    request = copy.deepcopy(fixtures["setup"]["first_agent"])
    original = json.dumps(request, indent=2).encode()
    pending = PendingSetup.capture(binding, original)
    request["agent"]["brief"]["text"] = "new selection must not change pending input"
    cancelled = pending.cancellation()
    assert pending.body == original
    assert json.loads(cancelled.body) == fixtures["setup"]["cancel_first_agent"]
    assert cancelled.binding is pending.binding is binding
    assert cancelled.request_id == pending.request_id
    assert cancelled.cancellation() is cancelled
    assert "Project work" not in repr(pending)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"schema_version": 1}, "invalid_request"),
        ({"data_dir": "/synthetic/forbidden"}, "invalid_request"),
        ({"profile_id": "other"}, "binding_mismatch"),
        ({"expected_installation_id": "other"}, "binding_mismatch"),
        ({"expected_lifecycle_id": "other"}, "binding_mismatch"),
        ({"action": "initialize"}, "invalid_request"),
    ],
)
def test_request_refuses_before_transport(fixtures, binding, change, code):
    request = {**fixtures["setup"]["first_agent"], **change}
    with pytest.raises(Refused) as error:
        PendingSetup.capture(binding, json.dumps(request).encode())
    assert error.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "mismatch",
        "unauthorized",
        "disconnect",
        "redirect",
        "wrong_action",
        "invalid_json",
        "oversize",
    ],
)
async def test_private_uds_setup_is_once_and_unknown_stays_uncertain(fixtures, case):
    requests = []
    closed = asyncio.Event()
    result = {
        key: value
        for key, value in fixtures["setup"]["result"].items()
        if key in {"action", "status", "code", "message", "retry", "completed_steps", "data"}
    }
    if case == "wrong_action":
        result["action"] = "cancel_first_agent"
    body = json.dumps(result).encode()
    if case == "invalid_json":
        body = b"not-json"
    if case == "oversize":
        body = b"x" * 65537
    status = 200
    if case == "mismatch":
        status = 409
        body = b'{"detail":{"code":"binding_mismatch","message":"Profile binding does not match."}}'
    if case == "unauthorized":
        status = 401
    if case == "redirect":
        status = 302

    async def serve(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            if head.startswith(b"GET"):
                payload = json.dumps(fixtures["identity"]).encode()
                current_status = 200
            else:
                size = next(
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                requests.append(await reader.readexactly(size))
                if case == "disconnect":
                    return
                payload, current_status = body, status
            writer.write(
                (
                    f"HTTP/1.1 {current_status} Test\r\nContent-Length: {len(payload)}\r\n"
                    + "Connection: close\r\nLocation: http://127.0.0.1:1/elsewhere\r\n\r\n"
                ).encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            if requests:
                closed.set()

    with tempfile.TemporaryDirectory(prefix="bound-", dir="/tmp") as temporary:
        path = str(Path(temporary) / "api.sock")
        record = copy.deepcopy(fixtures["discovery"]["show_ready"])
        record["binding"]["endpoint"]["path"] = path
        bound = Binding.capture(
            record,
            ("profile-a", "installation-a", "lifecycle-a1"),
            lambda _: "synthetic-credential",
        )
        pending = PendingSetup.capture(bound, json.dumps(fixtures["setup"]["first_agent"]).encode())
        server = await asyncio.start_unix_server(serve, path=path)
        async with server:
            if case == "valid":
                assert await pending.execute() == result
            else:
                with pytest.raises(Refused) as error:
                    await pending.execute()
                if case in {"mismatch", "unauthorized"}:
                    assert not error.value.uncertain
                else:
                    assert error.value.code == "outcome_unknown" and error.value.uncertain
            await asyncio.wait_for(closed.wait(), 2)
        assert requests == [pending.body]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_non_utf8_input_is_refused_before_cancellation_can_expand_it(fixtures, binding, encoding):
    with pytest.raises(Refused) as error:
        PendingSetup.capture(binding, json.dumps(fixtures["setup"]["first_agent"]).encode(encoding))
    assert error.value.code == "invalid_request"


def test_unicode_cancellation_retains_values_without_exceeding_action_allowance(fixtures, binding):
    from agent_backbone.models import SETUP_BODY_BYTES, SETUP_CANCEL_BODY_BYTES

    request = copy.deepcopy(fixtures["setup"]["first_agent"])
    request["agent"]["brief"]["text"] = "測試" * 4000
    raw = json.dumps(request, ensure_ascii=False).encode()
    raw += b" " * (SETUP_BODY_BYTES - len(raw))
    pending = PendingSetup.capture(binding, raw)
    cancelled = pending.cancellation()
    assert len(cancelled.body) <= SETUP_CANCEL_BODY_BYTES
    assert json.loads(cancelled.body) == {**request, "action": "cancel_first_agent"}


def invoke_bound(tmp_path, capsys, body, *, validate=False):
    from agent_backbone.cli import build_parser

    source = tmp_path / "bound-request.json"
    raw = json.dumps(body, indent=2).encode()
    source.write_bytes(raw)
    args = build_parser().parse_args(
        ["setup", "--file", str(source), "--jsonl"] + (["--validate"] if validate else [])
    )
    code = args.func(args)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    for event in events:
        assert event["schema_version"] == 2
        assert event["request_id"] == body["request_id"]
        assert event["profile_id"] == body["profile_id"]
        assert event["installation_id"] == body["expected_installation_id"]
        assert event["lifecycle_id"] == body["expected_lifecycle_id"]
    return code, events, raw


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("action", ["first_agent", "cancel_first_agent"])
def test_bound_cli_preserves_raw_request_and_schema2_events(
    fixtures, binding, tmp_path, capsys, monkeypatch, runtime, action
):
    from agent_backbone.cli import _profile_client, onboarding

    body = copy.deepcopy(fixtures["setup"]["first_agent"])
    body["action"] = action
    body["agent"]["runtime"] = runtime
    body["hooks"] = [runtime]
    seen = []
    monkeypatch.setattr(_profile_client, "resolve_binding", lambda expected, **kwargs: binding)
    monkeypatch.setattr(onboarding, "_safe_directory", lambda _: pytest.fail("legacy path"))

    async def execute(self):
        seen.append(self)
        return {
            "action": self.action,
            "status": "completed",
            "code": "test_completed",
            "message": "Synthetic execution result.",
            "retry": "none",
            "completed_steps": ["preflight"],
            "data": {},
        }

    monkeypatch.setattr(PendingSetup, "execute", execute)
    code, events, raw = invoke_bound(tmp_path, capsys, body)
    assert code == 0
    assert len(seen) == 1 and seen[0].body == raw and seen[0].binding is binding
    assert all(event["action"] == action for event in events)
    assert events[-1]["code"] == "test_completed"


@pytest.mark.parametrize(
    "marker",
    [
        "BACKBONE_AGENT",
        "BACKBONE_CLIENT_AGENT",
        "BACKBONE_CLIENT_REGISTRATION",
        "BACKBONE_AGENT_CREDENTIAL",
    ],
)
def test_bound_cli_operator_admission_precedes_registry_and_credentials(
    fixtures, tmp_path, capsys, monkeypatch, marker
):
    from agent_backbone.services.database import ProfileRegistry

    monkeypatch.setenv(marker, "" if marker == "BACKBONE_AGENT_CREDENTIAL" else "worker")
    monkeypatch.setattr(ProfileRegistry, "show", lambda *a: pytest.fail("registry read"))
    code, events, _ = invoke_bound(tmp_path, capsys, fixtures["setup"]["first_agent"])
    assert code == 3 and events[-1]["code"] == "operator_required"


def test_bound_cli_validation_does_not_resolve_or_bootstrap(
    fixtures, tmp_path, capsys, monkeypatch
):
    from agent_backbone.services.database import ProfileRegistry

    monkeypatch.setattr(ProfileRegistry, "show", lambda *a: pytest.fail("registry read"))
    code, events, _ = invoke_bound(
        tmp_path, capsys, fixtures["setup"]["first_agent"], validate=True
    )
    assert code == 0 and events[-1]["code"] == "validated"


def test_bound_cli_nonready_authoritative_registry_does_not_read_credentials(
    fixtures, tmp_path, capsys, monkeypatch
):
    from agent_backbone import config
    from agent_backbone.cli import _profile_client
    from agent_backbone.services.database import (
        ProfileIdentity,
        ProfileRegistry,
        ProfileReservation,
    )

    registry = ProfileRegistry.create(tmp_path / "registry" / "registry.db")
    registry.reserve(
        ProfileReservation(
            ProfileIdentity("profile-a", "installation-a", "lifecycle-a1"),
            tmp_path / "provider",
            "Work",
            "baseline-v1",
        )
    )
    monkeypatch.setattr(config, "PROFILE_REGISTRY_PATH", str(registry.path))
    monkeypatch.setattr(
        _profile_client, "resolve_dotenv_reference", lambda *a: pytest.fail("secret read")
    )
    before = registry.path.read_bytes()
    code, events, _ = invoke_bound(tmp_path, capsys, fixtures["setup"]["first_agent"])
    assert code == 3 and events[-1]["code"] == "service_unavailable"
    assert registry.path.read_bytes() == before
    assert not (tmp_path / "provider").exists()


@pytest.fixture
def local_connection(fixtures, tmp_path, monkeypatch):
    from agent_backbone import config
    from agent_backbone.services.database import (
        ProfileIdentity,
        ProfileRegistry,
        ProfileReservation,
    )

    registry = ProfileRegistry.create(tmp_path / "registry" / "registry.db")
    root = tmp_path / "provider"
    root.mkdir(mode=0o700)
    registry.reserve(
        ProfileReservation(
            ProfileIdentity("profile-a", "installation-a", "lifecycle-a1"),
            root,
            "Work",
            "baseline-v1",
        )
    )
    credential = root / ".env"
    credential.write_text("BACKBONE_API_KEY=synthetic-original-credential\n")
    credential.chmod(0o600)
    ready = copy.deepcopy(fixtures["discovery"]["show_ready"])
    ready["binding"]["endpoint"]["path"] = str(root / "api.sock")
    ready["binding"]["tmux"]["socket_path"] = str(root / "tmux.sock")
    ready["binding"]["credential_reference"]["path"] = str(credential)
    # The production registry remains non-ready. Supply only its future ready
    # discovery shape; the real registry and durable continuity files are used.
    monkeypatch.setattr(ProfileRegistry, "show", lambda *a: copy.deepcopy(ready))
    monkeypatch.setattr(config, "PROFILE_REGISTRY_PATH", str(registry.path))
    return registry, ready, credential


@pytest.mark.parametrize(
    "change",
    ["credential", "reference", "endpoint", "tmux", "body", "corrupt", "missing_connection"],
)
def test_new_invocation_cannot_rebind_original_setup(fixtures, local_connection, change):
    from agent_backbone.cli._profile_client import resolve_binding

    registry, ready, credential = local_connection
    request = copy.deepcopy(fixtures["setup"]["first_agent"])
    expected = ("profile-a", "installation-a", "lifecycle-a1")
    first = resolve_binding(expected, setup_request=request)
    first.setup_connection(request, capture=True)
    files = list(
        registry.path.parent.glob(
            "registry.db.operations/setup-connections/*/setup-receipts/*.json"
        )
    )
    assert len(files) == 1
    original = files[0].read_bytes()
    assert first.credential.encode() not in original
    assert request["agent"]["brief"]["text"].encode() not in original
    assert resolve_binding(expected, setup_request=request) == first
    cancel = {**request, "action": "cancel_first_agent"}
    assert resolve_binding(expected, setup_request=cancel) == first
    assert files[0].read_bytes() == original
    if change == "credential":
        credential.write_text("BACKBONE_API_KEY=synthetic-replacement-credential\n")
    elif change == "reference":
        new = credential.with_name("replacement.env")
        new.write_bytes(credential.read_bytes())
        new.chmod(0o600)
        ready["binding"]["credential_reference"]["path"] = str(new)
    elif change == "endpoint":
        ready["binding"]["endpoint"]["path"] += ".replacement"
    elif change == "tmux":
        ready["binding"]["tmux"]["socket_path"] += ".replacement"
    elif change == "body":
        cancel["start_greeting"] = not cancel["start_greeting"]
    elif change == "corrupt":
        files[0].write_bytes(b"{")
    else:
        record = json.loads(original)
        del record["connection"]
        files[0].write_text(json.dumps(record))
    before = files[0].read_bytes()
    with pytest.raises(Refused) as failure:
        resolve_binding(expected, setup_request=cancel)
    assert failure.value.code == (
        "request_conflict"
        if change == "body"
        else "installation_conflict"
        if change == "corrupt"
        else "binding_mismatch"
    )
    assert files[0].read_bytes() == before


def test_cancel_first_claims_same_connection_and_conflicting_capture_is_busy(
    fixtures, local_connection
):
    from agent_backbone.cli._profile_client import resolve_binding

    registry, ready, credential = local_connection
    original = fixtures["setup"]["first_agent"]
    expected = ("profile-a", "installation-a", "lifecycle-a1")
    cancellation = {**original, "action": "cancel_first_agent"}
    bound = resolve_binding(expected, setup_request=cancellation)
    bound.setup_connection(cancellation, capture=True)
    assert resolve_binding(expected, setup_request=original) == bound
    connection = json.loads(
        next(
            registry.path.parent.glob(
                "registry.db.operations/setup-connections/*/setup-receipts/*.json"
            )
        ).read_text()
    )["connection"]
    with registry.setup_connection_receipt(original, connection):
        ready["binding"]["endpoint"]["path"] += ".other"
        with pytest.raises(Refused) as failure:
            resolve_binding(expected, setup_request=original)
        assert failure.value.code == "outcome_unknown"
    with pytest.raises(Refused) as failure:
        resolve_binding(expected, setup_request=original)
    assert failure.value.code == "binding_mismatch"


def test_unknown_dispatch_keeps_connection_for_next_process(
    fixtures, local_connection, tmp_path, capsys, monkeypatch
):
    registry, _, credential = local_connection
    calls = []

    async def handshake(self):
        pass

    async def uncertain(self):
        calls.append(self)
        assert (
            len(
                list(
                    registry.path.parent.glob(
                        "registry.db.operations/setup-connections/*/setup-receipts/*.json"
                    )
                )
            )
            == 1
        )
        raise Refused("outcome_unknown", uncertain=True)

    monkeypatch.setattr(Binding, "handshake", handshake)
    monkeypatch.setattr(PendingSetup, "_post", uncertain)
    request = fixtures["setup"]["first_agent"]
    code, events, _ = invoke_bound(tmp_path, capsys, request)
    assert code == 3 and events[-1]["code"] == "outcome_unknown"
    credential.write_text("BACKBONE_API_KEY=synthetic-rotated-credential\n")
    code, events, _ = invoke_bound(tmp_path, capsys, request)
    assert code == 3 and events[-1]["code"] == "binding_mismatch"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["credential", "endpoint", "tmux"])
def test_refused_initial_handshake_does_not_pin_an_unverified_connection(
    fixtures, local_connection, tmp_path, capsys, monkeypatch, change
):
    registry, ready, credential = local_connection
    observations = []

    async def handshake(self):
        observations.append(self)
        if len(observations) == 1:
            raise Refused("operator_required" if change == "credential" else "binding_mismatch")

    async def post(self):
        assert len(list(registry.path.parent.glob("**/setup-receipts/*.json"))) == 1
        return {
            "action": self.action,
            "status": "completed",
            "code": "test_completed",
            "message": "Synthetic result.",
            "retry": "none",
            "completed_steps": [],
            "data": {},
        }

    monkeypatch.setattr(Binding, "handshake", handshake)
    monkeypatch.setattr(PendingSetup, "_post", post)
    request = fixtures["setup"]["first_agent"]
    code, events, _ = invoke_bound(tmp_path, capsys, request)
    assert code == 3 and events[-1]["code"] in {"operator_required", "binding_mismatch"}
    assert not list(registry.path.parent.glob("**/setup-receipts/*.json"))
    if change == "credential":
        credential.write_text("BACKBONE_API_KEY=synthetic-corrected-credential\n")
    elif change == "endpoint":
        ready["binding"]["endpoint"]["path"] += ".corrected"
    else:
        ready["binding"]["tmux"]["socket_path"] += ".corrected"
    code, events, _ = invoke_bound(tmp_path, capsys, request)
    assert code == 0 and events[-1]["code"] == "test_completed"
    assert len(observations) == 2


def test_connection_capacity_and_capture_lock_are_independent_per_profile(
    fixtures, local_connection, monkeypatch
):
    from agent_backbone.services.database import (
        ProfileIdentity,
        ProfileReservation,
        SetupReceiptError,
        _setup_receipts,
    )

    registry, _, credential = local_connection
    registry.reserve(
        ProfileReservation(
            ProfileIdentity("profile-b", "installation-b", "lifecycle-b1"),
            credential.parent.with_name("provider-b"),
            "Other",
            "baseline-v1",
        )
    )
    monkeypatch.setattr(_setup_receipts, "SETUP_RECEIPTS", 1)
    first = fixtures["setup"]["first_agent"]
    second = {
        **first,
        "profile_id": "profile-b",
        "expected_installation_id": "installation-b",
        "expected_lifecycle_id": "lifecycle-b1",
    }
    connection = {"synthetic": "connection"}
    with registry.setup_connection_receipt(first, connection):
        with registry.setup_connection_receipt(second, connection):
            pass  # Both the quota and lock belong to their own profile.
    with pytest.raises(SetupReceiptError) as failure:
        with registry.setup_connection_receipt({**first, "request_id": "new-id"}, connection):
            pass
    assert failure.value.code == "installation_conflict"
    with registry.setup_connection_receipt(first, connection):
        pass  # The original ID remains usable at capacity.


async def test_concurrent_verified_connections_compare_again_before_post(
    fixtures, local_connection, monkeypatch
):
    from agent_backbone.cli._profile_client import resolve_binding

    _, _, credential = local_connection
    request = fixtures["setup"]["first_agent"]
    expected = ("profile-a", "installation-a", "lifecycle-a1")
    first = resolve_binding(expected, setup_request=request)
    credential.write_text("BACKBONE_API_KEY=synthetic-concurrent-credential\n")
    second = resolve_binding(expected, setup_request=request)
    posts = []

    async def handshake(self):
        await asyncio.sleep(0)

    async def post(self):
        posts.append(self.binding)
        await asyncio.sleep(0)
        raise Refused("outcome_unknown", uncertain=True)

    monkeypatch.setattr(Binding, "handshake", handshake)
    monkeypatch.setattr(PendingSetup, "_post", post)
    raw = json.dumps(request).encode()
    results = await asyncio.gather(
        PendingSetup.capture(first, raw).execute(),
        PendingSetup.capture(second, raw).execute(),
        return_exceptions=True,
    )
    assert [item.code for item in results] == ["outcome_unknown", "binding_mismatch"]
    assert posts == [first]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_schema1_preserves_existing_json_encoding_acceptance(tmp_path, capsys, encoding):
    from agent_backbone.cli import build_parser

    source = tmp_path / "legacy.json"
    source.write_bytes(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": "legacy-encoding",
                "action": "initialize",
                "service": "none",
                "starter_profile": "core-v1",
                "data_dir": str(tmp_path / "provider"),
            }
        ).encode(encoding)
    )
    args = build_parser().parse_args(["setup", "--file", str(source), "--jsonl", "--validate"])
    assert args.func(args) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["schema_version"] == 1 and events[-1]["code"] == "validated"


@pytest.mark.parametrize("malformed", ["oversize_first", "utf-8-sig", "utf-16", "utf-32"])
def test_early_bound_validation_refusal_retains_known_identity(
    fixtures, tmp_path, capsys, monkeypatch, malformed
):
    from agent_backbone.cli import build_parser
    from agent_backbone.models import SETUP_BODY_BYTES, SETUP_CANCEL_BODY_BYTES
    from agent_backbone.services.database import ProfileRegistry

    monkeypatch.setattr(ProfileRegistry, "show", lambda *a: pytest.fail("registry read"))
    request = fixtures["setup"]["first_agent"]
    raw = json.dumps(request).encode("utf-8" if malformed == "oversize_first" else malformed)
    if malformed == "oversize_first":
        raw += b" " * (SETUP_BODY_BYTES + 1 - len(raw))
    assert len(raw) <= SETUP_CANCEL_BODY_BYTES
    source = tmp_path / "invalid-bound.json"
    source.write_bytes(raw)
    args = build_parser().parse_args(["setup", "--file", str(source), "--jsonl"])
    assert args.func(args) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 1
    assert events[0]["schema_version"] == 2
    assert events[0]["request_id"] == request["request_id"]
    assert events[0]["action"] == request["action"]
    assert events[0]["profile_id"] == request["profile_id"]
    assert events[0]["installation_id"] == request["expected_installation_id"]
    assert events[0]["lifecycle_id"] == request["expected_lifecycle_id"]
    assert events[0]["code"] == "invalid_request"


def test_bound_capacity_refusal_explains_permanent_limit_without_advising_retry(
    fixtures, local_connection, tmp_path, capsys, monkeypatch
):
    posts = []
    monkeypatch.setattr("agent_backbone.services.database._setup_receipts.SETUP_RECEIPTS", 1)

    async def handshake(self):
        pass

    async def post(self):
        posts.append(self.request_id)
        return {
            "action": self.action,
            "status": "completed",
            "code": "test_completed",
            "message": "Synthetic result.",
            "retry": "none",
            "completed_steps": [],
            "data": {},
        }

    monkeypatch.setattr(Binding, "handshake", handshake)
    monkeypatch.setattr(PendingSetup, "_post", post)
    original = fixtures["setup"]["first_agent"]
    assert invoke_bound(tmp_path, capsys, original)[0] == 0
    code, events, _ = invoke_bound(tmp_path, capsys, {**original, "request_id": "excess-id"})
    assert code == 3
    assert events[-1]["code"] == "installation_conflict"
    assert events[-1]["retry"] == "none"
    assert "capacity reached" in events[-1]["message"]
    assert "existing request IDs remain usable" in events[-1]["message"]
    assert posts == [original["request_id"]]
    assert invoke_bound(tmp_path, capsys, original)[0] == 0


@pytest.mark.parametrize("action", ["initialize", "first_agent"])
@pytest.mark.parametrize("validate", [False, True])
def test_legacy_oversize_failure_keeps_original_unknown_envelope(
    tmp_path, capsys, action, validate
):
    from agent_backbone.cli import build_parser
    from agent_backbone.models import SETUP_BODY_BYTES, SETUP_CANCEL_BODY_BYTES

    raw = json.dumps(
        {"schema_version": 1, "request_id": "oversized-legacy", "action": action}
    ).encode()
    raw += b" " * (SETUP_BODY_BYTES + 1 - len(raw))
    assert len(raw) <= SETUP_CANCEL_BODY_BYTES
    source = tmp_path / "legacy-large.json"
    source.write_bytes(raw)
    args = build_parser().parse_args(
        ["setup", "--file", str(source), "--jsonl"] + (["--validate"] if validate else [])
    )
    assert args.func(args) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 1
    assert events[0]["schema_version"] == 1
    assert events[0]["request_id"] == "unknown"
    assert events[0]["action"] == "validate"
    assert events[0]["code"] == "invalid_request"
