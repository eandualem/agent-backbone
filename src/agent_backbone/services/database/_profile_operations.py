"""Durable local lifecycle intent, independent of the provider being operated on."""

import json

from agent_backbone.services.database._profile_registry import ProfileRegistry

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


class ProfileOperations:
    """Reserve before effects; never repeat unknown work after a process crash.

    A pending row serializes its target across processes. It survives caller
    timeout and registry reopen. Replaying pending input returns that receipt;
    it is not permission to execute the effects again. Explicit recovery must
    reconcile the original operation before releasing its pending reservation.
    """

    def __init__(self, registry: ProfileRegistry):
        self.registry = registry

    def lookup(self, request_id: str) -> dict:
        with self.registry._connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
                return operation_error(request_id, "operation_not_found")
            row = conn.execute(
                "SELECT receipt_json FROM lifecycle_operations WHERE request_id = ?", (request_id,)
            ).fetchone()
            return json.loads(row[0]) if row else operation_error(request_id, "operation_not_found")

    def begin(self, request: dict) -> tuple[dict, bool]:
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
            if conn.execute("PRAGMA user_version").fetchone()[0] != 3:
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
        return receipt, not bool(pending)

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
        """Retain attempts and refuse effects until their prerequisites are available.

        No baseline is shipped and no activation or original-provider capture is
        installed yet. These are durable failures, never success-shaped mocks.
        """
        receipt, execute = self.begin(request)
        if not execute:
            return receipt
        if request["action"] == "create":
            code = "baseline_unavailable"
        elif request["action"] == "adopt_default":
            code = "adoption_unavailable"
        else:
            code = "binding_mismatch"
            with self.registry._connection() as conn:
                records = self.registry._records(conn)
                for record in records:
                    identity = record.identity
                    if (
                        identity.profile_id == request["profile_id"]
                        and identity.installation_id == request["expected_installation_id"]
                        and identity.lifecycle_id == request["expected_lifecycle_id"]
                        and record.state != "failed"
                    ):
                        code = "profile_not_ready"
                        break
        return self.fail(request, code)
