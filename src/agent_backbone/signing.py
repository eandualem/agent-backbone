"""Signed senders: the exact bytes an enrolled app signs, and their verification.

Pure functions with no Backbone state, so the API, the database and the
Telegram command all agree on one definition. ``docs/owner-confirmed-messages.md``
is the specification; ``tests/fixtures/signing_vectors.json`` pins it for both
this implementation and any app that signs.

Every framed text starts with a line naming what it is, then each field as
``<byte length>:<value>\\n``, so no field can run into the next one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import quote

REQUEST_V1 = "agent-backbone signed request v1"
ENROLL_PROOF_V1 = "agent-backbone enroll proof v1"
ROTATE_PROOF_V1 = "agent-backbone rotate proof v1"
TRANSITION_V1 = "agent-backbone key transition v1"

PURPOSES = ("request", "rotate")
SKEW_SECONDS = 300
NONCE_RE = re.compile(r"[0-9a-f]{32}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")


class VerifierUnavailable(RuntimeError):
    """Signatures can't be checked here; signed senders fail closed."""


def frame(first_line: str, fields: Sequence[str]) -> bytes:
    """``first_line\\n`` followed by each field as ``<byte length>:<value>\\n``."""
    out = [first_line.encode("utf-8"), b"\n"]
    for field in fields:
        raw = field.encode("utf-8")
        out += [str(len(raw)).encode("ascii"), b":", raw, b"\n"]
    return b"".join(out)


def canonical_query(params: Sequence[tuple[str, str]]) -> str:
    """Percent-encode each name and value (RFC 3986 unreserved characters kept,
    upper-case hex), sort by encoded name then encoded value, join with ``&``."""
    encoded = sorted((quote(name, safe="-._~"), quote(value, safe="-._~")) for name, value in params)
    return "&".join(f"{name}={value}" for name, value in encoded)


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_bytes(
    *,
    purpose: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    timestamp: int,
    nonce: str,
    audience: str,
    sender: str,
    epoch: int,
) -> bytes:
    """What an enrolled app signs for one request (``X-Backbone-Signature``)."""
    return frame(
        REQUEST_V1,
        [
            purpose,
            method.upper(),
            path,
            query,
            body_sha256(body),
            str(timestamp),
            nonce,
            audience,
            sender,
            str(epoch),
        ],
    )


def enroll_proof_bytes(
    *,
    sender: str,
    audience: str,
    action: str,
    expected_epoch: int,
    new_public_key: str,
    request_id: str,
) -> bytes:
    """What the new key signs to show the app holds it (an enrollment)."""
    return frame(
        ENROLL_PROOF_V1,
        [sender, audience, action, str(expected_epoch), new_public_key, request_id],
    )


def rotate_proof_bytes(
    *,
    sender: str,
    audience: str,
    old_epoch: int,
    new_public_key: str,
    nonce: str,
    timestamp: int,
) -> bytes:
    """What the new key signs in a rotation signed by the current key."""
    return frame(
        ROTATE_PROOF_V1,
        [sender, audience, str(old_epoch), new_public_key, nonce, str(timestamp)],
    )


def transition_digest(
    *,
    action: str,
    sender: str,
    audience: str,
    expected_epoch: int,
    new_fingerprint: str,
    request_id: str,
    expires_at: int,
) -> str:
    """The digest the owner copies from the app into ``/approve_key``."""
    framed = frame(
        TRANSITION_V1,
        [
            action,
            sender,
            audience,
            str(expected_epoch),
            new_fingerprint,
            request_id,
            str(expires_at),
        ],
    )
    return hashlib.sha256(framed).hexdigest()


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict base64url without padding; raises ValueError on anything else."""
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        raise ValueError("not base64url without padding")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64url without padding") from exc


def public_key(text: str) -> bytes:
    """A public key from its base64url text: exactly 32 raw bytes."""
    raw = b64url_decode(text)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return raw


def fingerprint(raw_public_key: bytes) -> str:
    return hashlib.sha256(raw_public_key).hexdigest()


def grouped(hex_text: str) -> str:
    """``3f2a9c1e 0b7d4e55 …``: how a fingerprint or digest is shown."""
    return " ".join(hex_text[i : i + 8] for i in range(0, len(hex_text), 8))


def verify(raw_public_key: bytes, signature: bytes, message: bytes) -> bool:
    """Whether ``signature`` is the key's Ed25519 signature over ``message``.

    Raises VerifierUnavailable when the ``cryptography`` package can't be
    loaded, so a caller refuses signed senders instead of admitting them."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - a core dependency
        raise VerifierUnavailable("the 'cryptography' package is not importable") from exc
    if len(signature) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(raw_public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def same_name(name: str) -> str:
    """The form names are compared in: NFKC, case folded, trimmed."""
    return unicodedata.normalize("NFKC", name).casefold().strip()


class DuplicateKey(ValueError):
    pass


def strict_json(body: bytes) -> object:
    """Parse JSON, refusing a duplicate key anywhere (ValueError)."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        seen: dict[str, object] = {}
        for key, value in items:
            if key in seen:
                raise DuplicateKey(f"duplicate key: {key}")
            seen[key] = value
        return seen

    return json.loads(body.decode("utf-8"), object_pairs_hook=pairs)
