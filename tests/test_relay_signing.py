"""The golden vector of the relay's request signing (SEC-021), checked against the engine's own code.

The relay is off in 0.2.0, and this repository has no tests of the relay worker. This file checks only the one thing
a fixture in the tree can prove offline: that the engine builds the same signed bytes, derives the same key id and
accepts the same signature as the vector, which was made with a throwaway Ed25519 key that was discarded. The website
(a private repository) checks the same vector on its side."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from locus import relay_signing as rs  # noqa: E402

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "relay" / "signing-vector-v1.json").read_text("utf-8"))


def _signature() -> bytes:
    return rs._b64decode(VECTOR["sig"].rstrip("="))


def _message() -> bytes:
    return rs.signing_message(
        kid=VECTOR["kid"],
        user_id=VECTOR["user_id"],
        request_id=VECTOR["request_id"],
        iat=VECTOR["iat"],
        exp=VECTOR["exp"],
        body=VECTOR["body"],
    )


def test_the_body_hash_and_the_signed_bytes_are_the_vectors():
    assert hashlib.sha256(VECTOR["body"].encode("utf-8")).hexdigest() == VECTOR["body_sha256"]
    assert _message() == VECTOR["message"].encode("utf-8")


def test_the_key_id_is_derived_from_the_public_key_the_vector_names():
    key = rs.parse_public_key(VECTOR["pub"])
    assert key.kid == VECTOR["kid"]


def test_the_signature_verifies_and_a_changed_byte_does_not():
    key = rs.parse_public_key(VECTOR["pub"])
    assert rs._verify(key, _signature(), _message()) is True
    tampered = bytearray(_message())
    tampered[-1] ^= 1
    assert rs._verify(key, _signature(), bytes(tampered)) is False
    assert rs._verify(key, bytes(64), _message()) is False


def test_a_public_key_that_is_not_32_bytes_is_refused():
    with pytest.raises(ValueError):
        rs.parse_public_key("AAAA")
