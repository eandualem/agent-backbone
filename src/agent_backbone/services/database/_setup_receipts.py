"""Bounded local installer receipts, independent of database initialization.

These are operation receipts, never configuration or a scheduled workflow.
Keep accepted identities indefinitely: deleting a completed receipt would make
an old request look new. At capacity, refuse new IDs instead of evicting them.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

SETUP_RECEIPTS = 1024
SETUP_RECEIPT_BYTES = 65_536


class SetupReceiptError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SetupReceipt:
    """One nonblocking installation lock and a request-bound durable receipt.

    The caller checks operator authority before opening this object. The same lock
    is used by the CLI initializer and the daemon's first-agent operation. A retry
    observes an in-flight request rather than blocking the daemon's event loop.
    """

    def __init__(self, data_dir: Path, request: dict):
        self.directory = data_dir / "setup-receipts"
        self.request_id = request["request_id"]
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        self.fingerprint = hashlib.sha256(canonical).hexdigest()
        self.path = self.directory / (
            hashlib.sha256(self.request_id.encode()).hexdigest() + ".json"
        )
        self.record: dict = {}
        self._lock: int | None = None

    def __enter__(self) -> SetupReceipt:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise SetupReceiptError("installation_conflict", "Setup receipts must be private.")
            self._lock = os.open(
                self.directory / "lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            info = os.fstat(self._lock)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("not a private regular lock")
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists() or self.path.is_symlink():
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("not a regular receipt")
                    raw = stream.read(SETUP_RECEIPT_BYTES + 1)
                if len(raw) > SETUP_RECEIPT_BYTES:
                    raise ValueError("receipt too large")
                self.record = json.loads(raw)
                if (
                    not isinstance(self.record, dict)
                    or not isinstance(self.record.get("completed_steps"), list)
                    or not all(isinstance(step, str) for step in self.record["completed_steps"])
                    or not isinstance(self.record.get("files", {}), dict)
                    or not isinstance(self.record.get("result", {}), dict)
                    or self.record.get("schema_version") != 1
                    or self.record.get("request_id") != self.request_id
                ):
                    raise ValueError("invalid receipt")
                if self.record.get("fingerprint") != self.fingerprint:
                    raise SetupReceiptError(
                        "request_conflict", "This request ID already belongs to different input."
                    )
            else:
                if sum(1 for _ in self.directory.glob("*.json")) >= SETUP_RECEIPTS:
                    raise SetupReceiptError(
                        "installation_conflict",
                        "Setup receipt capacity reached; existing IDs remain usable.",
                    )
                self.record = {
                    "schema_version": 1,
                    "request_id": self.request_id,
                    "fingerprint": self.fingerprint,
                    "completed_steps": [],
                }
                self.save()
            return self
        except BlockingIOError as exc:
            self.__exit__()
            raise SetupReceiptError(
                "outcome_unknown", "Another setup operation is still running."
            ) from exc
        except SetupReceiptError:
            self.__exit__()
            raise
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.__exit__()
            raise SetupReceiptError(
                "installation_conflict", "Cannot read the setup receipt safely."
            ) from exc

    def save(self, **updates) -> None:
        self.record.update(updates)
        raw = (json.dumps(self.record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(raw) > SETUP_RECEIPT_BYTES:
            raise SetupReceiptError(
                "installation_conflict", "Setup receipt exceeds its size limit."
            )
        fd, name = tempfile.mkstemp(prefix=".receipt-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            parent = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(name).unlink(missing_ok=True)

    def completed(self, step: str) -> None:
        steps = self.record["completed_steps"]
        if step not in steps:
            steps.append(step)
        self.save(pending=None)

    def __exit__(self, *args) -> None:
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None
