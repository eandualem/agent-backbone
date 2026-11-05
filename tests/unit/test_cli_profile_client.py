import asyncio
import copy
import json
import os
import tempfile
from pathlib import Path

import pytest

from agent_backbone.cli._profile_client import Binding, Refused


@pytest.fixture
def fixtures():
    root = Path(__file__).resolve().parents[2]
    return json.loads((root / "docs/contracts/profiles-v1.json").read_text())


@pytest.fixture
def selected(fixtures):
    return copy.deepcopy(fixtures["discovery"]["show_ready"])


EXPECTED = ("profile-a", "installation-a", "lifecycle-a1")


def test_capture_retains_original_record_and_credential(selected):
    calls = []

    def resolve(reference):
        calls.append(dict(reference))
        reference["path"] = "/synthetic/replacement"
        selected["binding"]["endpoint"]["path"] = "/synthetic/elsewhere"
        return "synthetic-private-credential"

    bound = Binding.capture(selected, EXPECTED, resolve)
    assert len(calls) == 1
    assert bound.endpoint == "/example/profiles/a/run/api.sock"
    assert bound.credential_reference == ("dotenv", "/example/profiles/a/.env", "BACKBONE_API_KEY")
    assert "synthetic-private-credential" not in repr(bound)
    assert bound.headers()["Authorization"] == "Bearer synthetic-private-credential"


def test_binding_preserves_unicode_paths(selected):
    selected["binding"]["endpoint"]["path"] = "/example/ሥራ/api.sock"
    bound = Binding.capture(selected, EXPECTED, lambda _: "synthetic-credential")
    assert bound.endpoint == "/example/ሥራ/api.sock"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"schema_version": True}, "binding_mismatch"),
        ({"schema_version": 2}, "binding_mismatch"),
        ({"profile_id": "profile-b"}, "binding_mismatch"),
        ({"kind": "unknown"}, "binding_mismatch"),
        ({"state": "creating"}, "service_unavailable"),
        ({"state": "failed"}, "service_unavailable"),
        ({"binding": None}, "binding_mismatch"),
    ],
)
def test_record_refusal_precedes_any_credential_read(selected, change, code):
    selected.update(change)
    calls = []
    with pytest.raises(Refused) as error:
        Binding.capture(selected, EXPECTED, lambda reference: calls.append(reference))
    assert error.value.code == code and calls == []


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("installation_id", "installation-b", "binding_mismatch"),
        ("lifecycle_id", "lifecycle-a2", "binding_mismatch"),
        ("endpoint", {"kind": "http", "path": "https://example.invalid"}, "binding_mismatch"),
        ("endpoint", {"kind": "unix", "path": "/a/../b"}, "binding_mismatch"),
        (
            "credential_reference",
            {"kind": "environment", "key": "BACKBONE_API_KEY"},
            "credential_unavailable",
        ),
        (
            "credential_reference",
            {"kind": "dotenv", "path": "/example/.env", "key": "OTHER_KEY"},
            "credential_unavailable",
        ),
        ("tmux", {"socket_path": "relative"}, "binding_mismatch"),
    ],
)
def test_binding_refusal_precedes_any_credential_read(selected, field, value, code):
    selected["binding"][field] = value
    calls = []
    with pytest.raises(Refused) as error:
        Binding.capture(selected, EXPECTED, lambda reference: calls.append(reference))
    assert error.value.code == code and calls == []


@pytest.mark.parametrize("credential", [None, "", "synthetic\nheader", "with space", "x" * 4097])
def test_bad_credential_never_uses_environment(selected, credential, monkeypatch):
    monkeypatch.setenv("BACKBONE_API_KEY", "ambient-must-not-be-used")
    with pytest.raises(Refused) as error:
        Binding.capture(selected, EXPECTED, lambda reference: credential)
    assert error.value.code == "credential_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "wrong_profile",
        "wrong_installation",
        "wrong_lifecycle",
        "wrong_tmux",
        "wrong_schema",
        "missing_baseline",
        "duplicate_field",
        "oversize",
        "invalid_json",
        "unauthorized",
        "forbidden",
        "conflict",
        "redirect",
        "gzip",
        "extra_fields",
        "disconnect",
    ],
)
async def test_real_private_unix_handshake(selected, fixtures, case, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    identity = copy.deepcopy(fixtures["identity"])
    for suffix, field in [
        ("profile", "profile_id"),
        ("installation", "installation_id"),
        ("lifecycle", "lifecycle_id"),
    ]:
        if case == "wrong_" + suffix:
            identity[field] = "replacement"
    if case == "wrong_tmux":
        identity["tmux"]["socket_path"] = "/replacement/tmux"
    if case == "wrong_schema":
        identity["schema_version"] = True
    if case == "missing_baseline":
        identity.pop("baseline_version")
    body = json.dumps(
        {
            **identity,
            **({"unexpected": "synthetic-private-extra"} if case == "extra_fields" else {}),
        }
    ).encode()
    if case == "duplicate_field":
        body = body[:-1] + b',"schema_version":1}'
    if case == "oversize":
        body = b"x" * 65537
    if case == "invalid_json":
        body = b"not-json"
    status = {"unauthorized": 401, "forbidden": 403, "conflict": 409, "redirect": 302}.get(
        case, 200
    )
    headers = b""
    if case == "redirect":
        headers += b"Location: http://127.0.0.1:1/redirect\r\n"
    if case == "gzip":
        headers += b"Content-Encoding: gzip\r\n"
    requests = []
    finished = asyncio.Event()

    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            if case == "disconnect":
                return
            writer.write(
                (
                    f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n"
                    "Connection: close\r\n"
                ).encode()
                + headers
                + b"\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    with tempfile.TemporaryDirectory(prefix="bound-", dir="/tmp") as temporary:
        path = str(Path(temporary) / "api.sock")
        server = await asyncio.start_unix_server(serve, path=path)
        async with server:
            selected["binding"]["endpoint"]["path"] = path
            bound = Binding.capture(selected, EXPECTED, lambda _: "synthetic-credential")
            if case in {"valid", "extra_fields"}:
                assert await bound.handshake() == identity
            else:
                with pytest.raises(Refused) as error:
                    await bound.handshake()
                expected_code = {
                    "unauthorized": "operator_required",
                    "forbidden": "operator_required",
                    "redirect": "service_unavailable",
                    "disconnect": "service_unavailable",
                }.get(case, "binding_mismatch")
                assert error.value.code == expected_code
            await asyncio.wait_for(finished.wait(), 2)
        assert len(requests) == 1
        assert b"GET /api/profile HTTP/1.1" in requests[0]
        assert b"Bearer synthetic-credential" in requests[0]
        for value in EXPECTED:
            assert value.encode() in requests[0]
    assert not Path(path).exists()


@pytest.mark.parametrize(
    "content,expected",
    [
        ("BACKBONE_API_KEY=synthetic-key\n", "synthetic-key"),
        ('export BACKBONE_API_KEY="synthetic-key"\n', "synthetic-key"),
        ("BACKBONE_API_KEY=${AMBIENT_SECRET}\n", "${AMBIENT_SECRET}"),
        ("BACKBONE_API_KEY='synthetic-key' # comment\n", "synthetic-key"),
    ],
)
def test_credential_read_is_exact_and_never_interpolates(tmp_path, monkeypatch, content, expected):
    from agent_backbone.cli._profile_client import resolve_dotenv_reference

    monkeypatch.setenv("AMBIENT_SECRET", "must-not-be-read")
    path = tmp_path / ".env"
    path.write_text(content)
    path.chmod(0o600)
    reference = {"kind": "dotenv", "path": str(path), "key": "BACKBONE_API_KEY"}
    assert resolve_dotenv_reference(reference) == expected


@pytest.mark.parametrize(
    "content",
    [
        "",
        "OTHER_KEY=synthetic",
        "BACKBONE_API_KEY=",
        "BACKBONE_API_KEY",
        "BACKBONE_API_KEY=first\nBACKBONE_API_KEY=second",
        'BACKBONE_API_KEY="not closed',
        'BACKBONE_API_KEY="has space"',
        "x" * 16385,
    ],
)
def test_invalid_credential_file_never_falls_back(tmp_path, monkeypatch, content):
    from agent_backbone.cli._profile_client import resolve_dotenv_reference

    monkeypatch.setenv("BACKBONE_API_KEY", "ambient-must-not-be-used")
    path = tmp_path / ".env"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(Refused) as error:
        resolve_dotenv_reference({"kind": "dotenv", "path": str(path), "key": "BACKBONE_API_KEY"})
    assert error.value.code == "credential_unavailable"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "public", "missing"])
def test_credential_source_must_be_private_owned_regular_file(tmp_path, kind):
    from agent_backbone.cli._profile_client import resolve_dotenv_reference

    path = tmp_path / ".env"
    target = tmp_path / "target"
    target.write_text("BACKBONE_API_KEY=synthetic-key")
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "public":
        path.write_text("BACKBONE_API_KEY=synthetic-key")
        path.chmod(0o644)
    with pytest.raises(Refused) as error:
        resolve_dotenv_reference({"kind": "dotenv", "path": str(path), "key": "BACKBONE_API_KEY"})
    assert error.value.code == "credential_unavailable"


@pytest.mark.parametrize("prefix", ["backbone-agent-v1:", "backbone-agent-v2:"])
def test_scoped_agent_credential_cannot_become_operator(selected, tmp_path, prefix):
    from agent_backbone.cli._profile_client import resolve_dotenv_reference

    value = prefix + "synthetic"
    with pytest.raises(Refused):
        Binding.capture(selected, EXPECTED, lambda _: value)
    path = tmp_path / ".env"
    path.write_text("BACKBONE_API_KEY=" + value)
    path.chmod(0o600)
    with pytest.raises(Refused):
        resolve_dotenv_reference({"kind": "dotenv", "path": str(path), "key": "BACKBONE_API_KEY"})


def test_credential_ancestor_redirect_is_refused(tmp_path):
    from agent_backbone.cli._profile_client import resolve_dotenv_reference

    target = tmp_path / "target"
    target.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(target, target_is_directory=True)
    path = target / ".env"
    path.write_text("BACKBONE_API_KEY=synthetic-key")
    path.chmod(0o600)
    with pytest.raises(Refused) as error:
        resolve_dotenv_reference(
            {"kind": "dotenv", "path": str(redirected / ".env"), "key": "BACKBONE_API_KEY"}
        )
    assert error.value.code == "credential_unavailable"


@pytest.mark.asyncio
async def test_cancelling_handshake_closes_connection_without_retry(selected):
    received = asyncio.Event()
    disconnected = asyncio.Event()
    requests = []

    async def serve(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            received.set()
            assert await asyncio.wait_for(reader.read(1), 2) == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    with tempfile.TemporaryDirectory(prefix="bound-", dir="/tmp") as temporary:
        path = str(Path(temporary) / "api.sock")
        server = await asyncio.start_unix_server(serve, path=path)
        async with server:
            selected["binding"]["endpoint"]["path"] = path
            bound = Binding.capture(selected, EXPECTED, lambda _: "synthetic-credential")
            task = asyncio.create_task(bound.handshake())
            await asyncio.wait_for(received.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(disconnected.wait(), 2)
        assert len(requests) == 1
    assert not Path(path).exists()


@pytest.mark.parametrize("mutation", ["replace", "permissions", "hardlink"])
def test_credential_changed_during_read_is_refused(tmp_path, monkeypatch, mutation):
    from agent_backbone.cli._profile_client import resolve_dotenv_reference

    path = tmp_path / ".env"
    path.write_text("BACKBONE_API_KEY=synthetic-key")
    path.chmod(0o600)
    original = os.read
    changed = False

    def read(descriptor, size):
        nonlocal changed
        result = original(descriptor, size)
        if result and not changed:
            changed = True
            if mutation == "replace":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(0o600)
                replacement.replace(path)
            elif mutation == "permissions":
                path.chmod(0o644)
            else:
                os.link(path, tmp_path / "alias")
        return result

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(Refused) as error:
        resolve_dotenv_reference({"kind": "dotenv", "path": str(path), "key": "BACKBONE_API_KEY"})
    assert error.value.code == "credential_unavailable" and changed


@pytest.mark.asyncio
async def test_handshake_deadline_closes_incomplete_response(selected, monkeypatch):
    from agent_backbone.cli import _profile_client as client

    monkeypatch.setattr(client, "HANDSHAKE_SECONDS", 0.05)
    disconnected = asyncio.Event()

    async def serve(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(1), 2) == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    with tempfile.TemporaryDirectory(prefix="bound-", dir="/tmp") as temporary:
        path = str(Path(temporary) / "api.sock")
        server = await asyncio.start_unix_server(serve, path=path)
        async with server:
            selected["binding"]["endpoint"]["path"] = path
            bound = Binding.capture(selected, EXPECTED, lambda _: "synthetic-credential")
            with pytest.raises(Refused) as error:
                await asyncio.wait_for(bound.handshake(), 1)
            assert error.value.code == "service_unavailable"
            await asyncio.wait_for(disconnected.wait(), 2)
