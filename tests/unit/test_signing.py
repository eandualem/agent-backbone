"""The signing framing, checked against the shared vectors an app also runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_backbone import signing

VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "signing_vectors.json").read_text("utf-8")
)
KEYS = {name: signing.public_key(k["public_key"]) for name, k in VECTORS["keys"].items()}


def _signer(name: str) -> Ed25519PrivateKey:
    seed = signing.b64url_decode(VECTORS["keys"][name]["seed"])
    return Ed25519PrivateKey.from_private_bytes(seed)


def _framed(inp: dict) -> bytes:
    return signing.request_bytes(
        purpose=inp["purpose"],
        method=inp["method"],
        path=inp["path"],
        query=signing.canonical_query([tuple(p) for p in inp["query_params"]]),
        body=inp["body"].encode("utf-8"),
        timestamp=inp["timestamp"],
        nonce=inp["nonce"],
        audience=inp["audience"],
        sender=inp["sender"],
        epoch=inp["epoch"],
    )


@pytest.mark.parametrize("vector", VECTORS["requests"], ids=lambda v: v["name"])
def test_request_vectors(vector):
    inp = vector["input"]
    assert inp["body"].encode("utf-8").hex() == inp["body_hex"]
    assert (
        signing.canonical_query([tuple(p) for p in inp["query_params"]])
        == vector["canonical_query"]
    )
    framed = _framed(inp)
    assert framed.hex() == vector["signed_bytes_hex"]
    signature = signing.b64url_decode(vector["signature"])
    assert signing.verify(KEYS[vector["key"]], signature, framed)
    assert _signer(vector["key"]).sign(framed) == signature  # Ed25519 is deterministic


@pytest.mark.parametrize("case", VECTORS["negative"], ids=lambda c: c["name"])
def test_a_changed_request_no_longer_verifies(case):
    base = next(v for v in VECTORS["requests"] if v["name"] == case["base"])
    framed = _framed({**base["input"], **case["change"]})
    signature = signing.b64url_decode(base["signature"])
    assert not signing.verify(KEYS[base["key"]], signature, framed)


def test_proof_and_digest_vectors():
    for vector in VECTORS["enroll_proofs"]:
        framed = signing.enroll_proof_bytes(**vector["input"])
        assert framed.hex() == vector["signed_bytes_hex"]
        new_key = signing.public_key(vector["input"]["new_public_key"])
        assert signing.verify(new_key, signing.b64url_decode(vector["proof"]), framed)
    for vector in VECTORS["rotate_proofs"]:
        framed = signing.rotate_proof_bytes(**vector["input"])
        assert framed.hex() == vector["signed_bytes_hex"]
        new_key = signing.public_key(vector["input"]["new_public_key"])
        assert signing.verify(new_key, signing.b64url_decode(vector["proof"]), framed)
    for vector in VECTORS["transition_digests"]:
        assert signing.transition_digest(**vector["input"]) == vector["digest"]
        assert signing.grouped(vector["digest"]) == vector["shown"]
    assert any(v["input"]["new_fingerprint"] == "none" for v in VECTORS["transition_digests"])


@pytest.mark.parametrize("case", VECTORS["malformed"], ids=lambda c: c["name"])
def test_a_duplicate_key_is_refused(case):
    with pytest.raises(signing.DuplicateKey):
        signing.strict_json(case["body"].encode("utf-8"))


def test_encodings_are_strict():
    with pytest.raises(ValueError):
        signing.b64url_decode("abc=")  # padding
    with pytest.raises(ValueError):
        signing.public_key(signing.b64url_encode(b"x" * 31))
    assert not signing.verify(KEYS["app"], b"\0" * 63, b"message")
    assert signing.same_name(" ＡＳＳＩＳＴＡＮＴ ") == signing.same_name("assistant")
