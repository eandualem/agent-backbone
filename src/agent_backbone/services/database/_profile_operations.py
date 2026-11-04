"""Durable local lifecycle intent, independent of the provider being operated on."""

import asyncio
import json
import os
import secrets
import uuid
from contextlib import suppress

from agent_backbone.config import SQLITE_FILENAME, sqlite_url
from agent_backbone.services.database._profile_identity_repo import ProfileIdentity
from agent_backbone.services.database._profile_provider import reconcile_profile_provider
from agent_backbone.services.database._profile_registry import (
    ProfileBinding,
    ProfileRegistry,
    ProfileReservation,
)
from agent_backbone.services.database.backbone_db import BackboneDB
from agent_backbone.templates import core_starter_content

BASELINE = "core-v1"
"""The one shipped baseline, what a fresh install starts from: the starter policies
and skills, fresh secrets and no agents, accounts or integrations."""
_PROFILE_ROOTS = ("home", "config", "data", "state", "cache", "run", "tmp", "tmux")
_UNIX_SOCKET_PATH_LIMIT = 104
"""The smaller of the macOS and Linux sun_path sizes, including the terminator."""

_FIELDS = {
    "create": {"display_name", "baseline_version"},
    "start": {"profile_id", "expected_installation_id", "expected_lifecycle_id"},
    "stop": {"profile_id", "expected_installation_id", "expected_lifecycle_id"},
    "adopt_default": {"expected_installation_id", "adoption_token"},
}
ERRORS = {
    "request_conflict": "Profile operation request does not match its original input.",
    "operation_busy": "Another profile lifecycle operation is pending.",
    "operation_not_found": "Profile operation was not found.",
    "binding_mismatch": "Profile binding does not match.",
    "profile_not_ready": "Profile is not ready for lifecycle operations.",
    "baseline_unavailable": "Profile baseline is not available.",
    "adoption_unavailable": "Original provider adoption context is unavailable.",
    "profile_create_failed": "Profile could not be created.",
    "profile_path_too_long": "Profile paths exceed the Unix socket length limit.",
    "service_start_failed": "Profile provider could not be started.",
    "service_stop_failed": "Profile provider could not be stopped.",
}


def validate_profile_operation(request: dict) -> dict:
    """Accept the agreed shapes only, before touching control-plane storage."""
    if not isinstance(request, dict):
        raise ValueError("Invalid profile operation request")
    action = request.get("action")
    if not isinstance(action, str) or action not in _FIELDS:
        raise ValueError("Unsupported profile operation action")
    if (
        type(request.get("schema_version")) is not int
        or request["schema_version"] != 1
        or set(request) != {"schema_version", "request_id", "action"} | _FIELDS[action]
    ):
        raise ValueError("Invalid profile operation request")
    for key, value in request.items():
        if key == "schema_version":
            continue
        if (
            not isinstance(value, str)
            or not value.strip()
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
            or (key != "display_name" and any(not 0x21 <= ord(c) <= 0x7E for c in value))
        ):
            raise ValueError("Invalid profile operation request")
    return dict(request)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def operation_error(request_id: str, code: str) -> dict:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "error": {"code": code, "message": ERRORS[code]},
    }


async def _provision_profile(
    registry: ProfileRegistry,
    reservation: ProfileReservation,
    binding: ProfileBinding,
    port: int,
) -> None:
    """Create and reconcile the provider's storage. Run once: nothing here is replayed."""
    root = reservation.data_dir
    root.parent.mkdir(mode=0o700, exist_ok=True)
    root.mkdir(mode=0o700)
    for name in _PROFILE_ROOTS:
        (root / name).mkdir(mode=0o700)
    descriptor = os.open(binding.credential, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as credential:
        credential.write(f"BACKBONE_API_KEY={secrets.token_urlsafe(32)}\n")
        credential.flush()
        os.fsync(descriptor)
    starter, files = core_starter_content()
    for relative, content in files.items():
        (root / relative).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / relative).write_text(content)
    db = BackboneDB(sqlite_url(root))
    await db.start()
    try:
        await db.signing.audience(reservation.identity.installation_id)
        await db.profile_identity.reserve(reservation.identity)
        await db.settings.set("backbone.port", port)
        await db.settings.set("agents.shared_policy", starter["policies"])
        await db.settings.set("skills.store", str(root / "skills"))
    finally:
        await db.stop()
    await reconcile_profile_provider(
        registry, reservation, root / SQLITE_FILENAME, binding.tmux_socket
    )


class ProfileOperations:
    """Reserve before effects; never repeat unknown work after a process crash.

    A pending row serializes its target across processes. It survives caller
    timeout and registry reopen. Replaying pending input returns that receipt;
    it is not permission to execute the effects again. Explicit recovery must
    reconcile the original operation before releasing its pending reservation.
    """

    def __init__(self, registry: ProfileRegistry, services=None):
        self.registry = registry
        self.services = services

    def lookup(self, request_id: str) -> dict:
        with self.registry._connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
                return operation_error(request_id, "operation_not_found")
            row = conn.execute(
                "SELECT receipt_json FROM lifecycle_operations WHERE request_id = ?", (request_id,)
            ).fetchone()
            return json.loads(row[0]) if row else operation_error(request_id, "operation_not_found")

    def begin(self, request: dict) -> tuple[dict, bool]:
        return self._record(request)

    def _record(self, request: dict, *, refuse=False) -> tuple[dict, bool]:
        request = validate_profile_operation(request)
        body = _json(request)
        action = request["action"]
        # Create requests reserve distinct identities later; adoption must also
        # serialize with itself before a profile ID has been allocated.
        scope = (
            "profile:" + request["profile_id"]
            if action in {"start", "stop"}
            else "adoption:" + request["expected_installation_id"]
            if action == "adopt_default"
            else "create:" + request["request_id"]
        )
        installation_id = request.get("expected_installation_id")
        bound = action in {"start", "stop"}
        receipt = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "action": action,
            "profile_id": request["profile_id"] if bound else None,
            "installation_id": request["expected_installation_id"] if bound else None,
            "lifecycle_id": request["expected_lifecycle_id"] if bound else None,
            "status": "pending",
            "error": None,
        }
        with self.registry._connection(write=True) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] not in {3, 4}:
                raise ValueError("Profile operations need explicit registry initialization")
            previous = conn.execute(
                "SELECT request_json, receipt_json FROM lifecycle_operations WHERE request_id = ?",
                (request["request_id"],),
            ).fetchone()
            if previous:
                if previous[0] != body:
                    return operation_error(request["request_id"], "request_conflict"), False
                return json.loads(previous[1]), False
            pending = conn.execute(
                "SELECT 1 FROM lifecycle_operations WHERE (scope = ? OR installation_id = ?) "
                "AND status = 'pending'",
                (scope, installation_id),
            ).fetchone()
            if pending:
                receipt.update(
                    status="failed", error=operation_error("", "operation_busy")["error"]
                )
            elif refuse and (code := self._refusal(request, conn)):
                receipt.update(status="failed", error=operation_error("", code)["error"])
            conn.execute(
                "INSERT INTO lifecycle_operations "
                "(request_id, request_json, scope, installation_id, status, receipt_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    request["request_id"],
                    body,
                    scope,
                    installation_id,
                    receipt["status"],
                    _json(receipt),
                ),
            )
        return receipt, receipt["status"] == "pending"

    def fail(self, request: dict, code: str) -> dict:
        """Finish the exact reserved input with a fixed, non-secret refusal."""
        request = validate_profile_operation(request)
        error = operation_error(request["request_id"], code)["error"]
        with self.registry._connection(write=True) as conn:
            row = conn.execute(
                "SELECT request_json, receipt_json FROM lifecycle_operations WHERE request_id = ?",
                (request["request_id"],),
            ).fetchone()
            if row is None:
                raise ValueError("Profile operation has not been reserved")
            if row[0] != _json(request):
                return operation_error(request["request_id"], "request_conflict")
            receipt = json.loads(row[1])
            if receipt["status"] == "pending":
                receipt.update(status="failed", error=error)
                conn.execute(
                    "UPDATE lifecycle_operations SET status = 'failed', receipt_json = ? "
                    "WHERE request_id = ?",
                    (_json(receipt), request["request_id"]),
                )
            return receipt

    def apply(self, request: dict) -> dict:
        """Execute what is available and durably refuse the rest.

        Only creating the shipped baseline, starting and stopping have effects. No other baseline is
        shipped and no activation or original-provider capture is installed yet.
        These are durable failures, never success-shaped mocks.
        """
        request = validate_profile_operation(request)
        if request["action"] == "create" and request["baseline_version"] == BASELINE:
            return self._create(request)
        if request["action"] in {"start", "stop"} and self.services is not None:
            return self._run_service(request)
        # Refusal has no effects to reconcile. Classify and retain its receipt
        # in one transaction so a failed read/write cannot leave a pending lock.
        return self._record(request, refuse=True)[0]

    def _run_service(self, request: dict) -> dict:
        receipt, owned = self._record(request, refuse=True)
        if not owned:
            return receipt
        action = request["action"]
        try:
            reservation = self.registry.ready_reservation(request["profile_id"])
            getattr(self.services, action)(reservation)
        except Exception:
            return self.fail(request, f"service_{action}_failed")
        with self.registry._connection(write=True) as conn:
            receipt = self._stored_receipt(conn, request)
            receipt["status"] = "succeeded"
            conn.execute(
                "UPDATE lifecycle_operations SET status = 'succeeded', receipt_json = ? "
                "WHERE request_id = ?",
                (_json(receipt), request["request_id"]),
            )
        return receipt

    def _create(self, request: dict) -> dict:
        """Reserve once, then provision. An interruption stays pending and non-ready."""
        receipt, owned = self.begin(request)
        if not owned:
            return receipt
        identity = ProfileIdentity(secrets.token_hex(6), str(uuid.uuid4()), str(uuid.uuid4()))
        root = self.registry.path.parent / "p" / identity.profile_id
        binding = ProfileBinding(
            root / "run" / "api.sock", root / ".env", root / "tmux" / "server.sock"
        )
        if any(
            len(os.fsencode(path)) >= _UNIX_SOCKET_PATH_LIMIT
            for path in (binding.endpoint, binding.tmux_socket)
        ):
            return self.fail(request, "profile_path_too_long")
        reservation = ProfileReservation(
            identity, root, request["display_name"], request["baseline_version"]
        )
        reserved = False
        try:
            port = self._reserve_create(request, reservation)
            reserved = True
            asyncio.run(_provision_profile(self.registry, reservation, binding, port))
            return self._publish_create(request, reservation, binding)
        except Exception:
            if reserved:
                with suppress(ValueError):
                    self.registry.invalidate(identity, root)
            return self.fail(request, "profile_create_failed")

    def _reserve_create(self, request: dict, reservation: ProfileReservation) -> int:
        identity = reservation.identity
        with self.registry._connection(write=True) as conn:
            self.registry._reserve(conn, reservation)
            port = self.registry._allocate_port(conn, identity.profile_id)
            receipt = self._stored_receipt(conn, request)
            receipt.update(
                profile_id=identity.profile_id,
                installation_id=identity.installation_id,
                lifecycle_id=identity.lifecycle_id,
            )
            conn.execute(
                "UPDATE lifecycle_operations SET installation_id = ?, receipt_json = ? "
                "WHERE request_id = ?",
                (identity.installation_id, _json(receipt), request["request_id"]),
            )
        return port

    def _publish_create(
        self, request: dict, reservation: ProfileReservation, binding: ProfileBinding
    ) -> dict:
        # One transaction: a ready profile never has a pending creation receipt.
        with self.registry._connection(write=True) as conn:
            self.registry._publish(conn, reservation.identity, reservation.data_dir, binding)
            receipt = self._stored_receipt(conn, request)
            receipt["status"] = "succeeded"
            conn.execute(
                "UPDATE lifecycle_operations SET status = 'succeeded', receipt_json = ? "
                "WHERE request_id = ?",
                (_json(receipt), request["request_id"]),
            )
            return receipt

    @staticmethod
    def _stored_receipt(conn, request: dict) -> dict:
        return json.loads(
            conn.execute(
                "SELECT receipt_json FROM lifecycle_operations WHERE request_id = ?",
                (request["request_id"],),
            ).fetchone()[0]
        )

    def _refusal(self, request, conn) -> str | None:
        if request["action"] == "create":
            return "baseline_unavailable"
        if request["action"] == "adopt_default":
            return "adoption_unavailable"
        for record in self.registry._records(conn):
            identity = record.identity
            if (
                identity.profile_id == request["profile_id"]
                and identity.installation_id == request["expected_installation_id"]
                and identity.lifecycle_id == request["expected_lifecycle_id"]
                and record.state != "failed"
            ):
                if record.state == "ready" and self.services is not None:
                    return None
                return "profile_not_ready"
        return "binding_mismatch"
