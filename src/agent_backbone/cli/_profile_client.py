"""Immutable operator connections resolved from authoritative local discovery."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

import httpx
from dotenv.parser import parse_stream

FIELDS = ("profile_id", "installation_id", "lifecycle_id")
HEADERS = ("X-Backbone-Profile-Id", "X-Backbone-Installation-Id", "X-Backbone-Lifecycle-Id")
MAX_RESPONSE = 65536
HANDSHAKE_SECONDS = 10


class Refused(ValueError):
    def __init__(self, code, *, uncertain=False):
        self.code = code
        self.uncertain = uncertain
        super().__init__("Bound request could not be completed.")


def _text(value, limit=4096):
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and all(32 <= ord(c) < 127 for c in value)
    )


def _path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise Refused("binding_mismatch")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value or value == "/":
        raise Refused("binding_mismatch")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response field")
        result[key] = value
    return result


@dataclass(frozen=True)
class Binding:
    profile_id: str
    installation_id: str
    lifecycle_id: str
    endpoint: str
    tmux_socket: str
    credential_reference: tuple[str, str, str]
    credential: str = field(repr=False)
    registry_path: str | None = field(default=None, repr=False)

    @classmethod
    def capture(cls, record, expected, resolve_credential):
        """Resolve exactly once after every non-secret field has been validated."""
        if (
            not isinstance(expected, tuple)
            or len(expected) != 3
            or not all(_text(v, MAX_RESPONSE) and not any(c.isspace() for c in v) for v in expected)
        ):
            raise Refused("invalid_request")
        if (
            not isinstance(record, dict)
            or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1
        ):
            raise Refused("binding_mismatch")
        if record.get("profile_id") != expected[0] or record.get("kind") not in (
            "isolated",
            "legacy_default",
        ):
            raise Refused("binding_mismatch")
        if record.get("state") != "ready":
            raise Refused("service_unavailable")
        bound = record.get("binding")
        if (
            not isinstance(bound, dict)
            or (bound.get("installation_id"), bound.get("lifecycle_id")) != expected[1:]
        ):
            raise Refused("binding_mismatch")
        endpoint = bound.get("endpoint")
        reference = bound.get("credential_reference")
        tmux = bound.get("tmux")
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != {"kind", "path"}
            or endpoint["kind"] != "unix"
        ):
            raise Refused("binding_mismatch")
        if (
            not isinstance(reference, dict)
            or set(reference) != {"kind", "path", "key"}
            or reference["kind"] != "dotenv"
            or reference["key"] != "BACKBONE_API_KEY"
        ):
            raise Refused("credential_unavailable")
        if not isinstance(tmux, dict) or set(tmux) != {"socket_path"}:
            raise Refused("binding_mismatch")
        endpoint_path = _path(endpoint["path"])
        socket_path = _path(tmux["socket_path"])
        credential_path = _path(reference["path"])
        if len({endpoint_path, socket_path, credential_path}) != 3:
            raise Refused("binding_mismatch")
        # Pass a new reference object; callback mutation cannot alter this binding.
        credential = resolve_credential(
            {"kind": "dotenv", "path": credential_path, "key": "BACKBONE_API_KEY"}
        )
        from agent_backbone.config import agent_credential

        if (
            not _text(credential)
            or any(c.isspace() for c in credential)
            or agent_credential(credential)
        ):
            raise Refused("credential_unavailable")
        return cls(
            *expected,
            endpoint_path,
            socket_path,
            ("dotenv", credential_path, "BACKBONE_API_KEY"),
            credential,
        )

    def headers(self):
        return {
            **dict(
                zip(
                    HEADERS, (self.profile_id, self.installation_id, self.lifecycle_id), strict=True
                )
            ),
            "Authorization": "Bearer " + self.credential,
        }

    def setup_connection(self, request, *, capture=False):
        """Compare before handshake; capture only after it succeeds, before POST."""
        if self.registry_path is None:
            return
        from agent_backbone.services.database import ProfileRegistry, SetupReceiptError

        connection = {
            "endpoint": self.endpoint,
            "tmux_socket": self.tmux_socket,
            "credential_reference": list(self.credential_reference),
            "credential_sha256": hashlib.sha256(self.credential.encode()).hexdigest(),
        }
        try:
            receipt = ProfileRegistry(Path(self.registry_path)).setup_connection_receipt(
                request, connection
            )
            if capture:
                with receipt:
                    pass
            elif receipt.path.exists() or receipt.path.is_symlink():
                with receipt.inspect_existing():
                    pass
        except SetupReceiptError as exc:
            raise Refused(exc.code) from None
        except (OSError, ValueError, RecursionError):
            raise Refused("binding_mismatch") from None

    def verify_identity(self, body):
        if (
            not isinstance(body, dict)
            or type(body.get("schema_version")) is not int
            or body["schema_version"] != 1
        ):
            raise Refused("binding_mismatch")
        if tuple(body.get(key) for key in FIELDS) != (
            self.profile_id,
            self.installation_id,
            self.lifecycle_id,
        ):
            raise Refused("binding_mismatch")
        if body.get("tmux") != {"socket_path": self.tmux_socket} or not _text(
            body.get("baseline_version"), 128
        ):
            raise Refused("binding_mismatch")

    async def handshake(self):
        """Bound the entire observation, including a peer that trickles bytes."""
        try:
            async with asyncio.timeout(HANDSHAKE_SECONDS):
                return await self._handshake()
        except TimeoutError:
            raise Refused("service_unavailable") from None

    async def _handshake(self):
        """Authenticate only the pinned Unix endpoint; never use proxies/redirects."""
        try:
            transport = httpx.AsyncHTTPTransport(uds=self.endpoint, retries=0)
            async with (
                httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False, timeout=10
                ) as client,
                client.stream(
                    "GET",
                    "http://bound-provider/api/profile",
                    headers={**self.headers(), "Accept-Encoding": "identity"},
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise Refused("operator_required")
                if response.status_code == 409:
                    raise Refused("binding_mismatch")
                if response.status_code != 200:
                    raise Refused("service_unavailable")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise Refused("binding_mismatch")
                raw = bytearray()
                async for chunk in response.aiter_raw(chunk_size=4096):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise Refused("binding_mismatch")
            body = json.loads(raw, object_pairs_hook=_object)
            self.verify_identity(body)
            return {
                "schema_version": 1,
                **{key: body[key] for key in FIELDS},
                "baseline_version": body["baseline_version"],
                "tmux": {"socket_path": self.tmux_socket},
            }
        except Refused:
            raise
        except (httpx.HTTPError, OSError):
            raise Refused("service_unavailable") from None
        except (ValueError, RecursionError):
            raise Refused("binding_mismatch") from None


def resolve_dotenv_reference(reference):
    """Read one private operator file without interpolation or ambient fallback.

    A ready registry supplies the path authority; callers must enforce their local
    operator boundary before discovery. This function does not bootstrap a DB.
    """
    if (
        not isinstance(reference, dict)
        or set(reference) != {"kind", "path", "key"}
        or reference["kind"] != "dotenv"
        or reference["key"] != "BACKBONE_API_KEY"
    ):
        raise Refused("credential_unavailable")
    path = _path(reference["path"])
    descriptor = None
    try:
        if Path(path).resolve() != Path(path):
            raise Refused("credential_unavailable")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise Refused("credential_unavailable")
        chunks = []
        size = 0
        while size <= 16384:
            chunk = os.read(descriptor, 16385 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if Path(path).resolve() != Path(path):
            raise Refused("credential_unavailable")

        def signature(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                info.st_uid,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if (
            size > 16384
            or signature(before) != signature(after)
            or signature(after) != signature(current)
        ):
            raise Refused("credential_unavailable")
        raw = b"".join(chunks).decode("utf-8")
        values = []
        for entry in parse_stream(io.StringIO(raw)):
            if entry.error:
                raise Refused("credential_unavailable")
            if entry.key == "BACKBONE_API_KEY":
                values.append(entry.value)
        if len(values) != 1 or not _text(values[0]) or any(c.isspace() for c in values[0]):
            raise Refused("credential_unavailable")
        from agent_backbone.config import agent_credential

        if agent_credential(values[0]):
            raise Refused("credential_unavailable")
        return values[0]
    except Refused:
        raise
    except (OSError, ValueError, RecursionError):
        raise Refused("credential_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def resolve_binding(expected, *, setup_request=None):
    """Admit the local operator before reading registry or provider credentials."""
    from agent_backbone.cli.onboarding import _operator_environment
    from agent_backbone.config import PROFILE_REGISTRY_PATH
    from agent_backbone.services.database import ProfileRegistry, SetupReceiptError

    try:
        _operator_environment()
    except SetupReceiptError:
        raise Refused("operator_required") from None
    if (
        not isinstance(expected, tuple)
        or len(expected) != 3
        or not all(
            _text(value, MAX_RESPONSE) and not any(c.isspace() for c in value) for value in expected
        )
    ):
        raise Refused("invalid_request")
    try:
        registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
        record = registry.show(expected[0])
    except (OSError, ValueError, RecursionError):
        raise Refused("service_unavailable") from None
    binding = Binding.capture(record, expected, resolve_dotenv_reference)
    if setup_request is not None:
        binding = replace(binding, registry_path=str(registry.path))
        binding.setup_connection(setup_request)
    return binding
