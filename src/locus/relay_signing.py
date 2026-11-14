"""Relay request verification, engine side (SEC-021; master plan Ch.10.5 item 3, Appendix A).

An entitled key holder can mint a Realtime JWT and publish `mcp_request` on the relay channel
directly, skipping every check of the web's /api/mcp/relay (entitlement, kill switch, rate limit,
64 KB body cap). The relay therefore signs each request it forwards with Ed25519
(locus-web/lib/relay-signing.ts), and `locus serve --transport relay` runs only a request that:

1. carries `signed` = {v: 1, alg: "Ed25519", kid, iat, exp, body, sig};
2. is signed by a key this engine trusts: the public keys /api/relay/token returns as
   `request_signing_keys` (re-read at every token refresh, which is how a rotation arrives), or
   only the keys in LOCUS_RELAY_VERIFY_KEYS when that is set (a pin);
3. verifies over "locus-relay-request/v1", kid, THIS account's user id, request_id, iat, exp and
   sha256(body), one per line: a request signed for another account, or with anything changed,
   fails here;
4. is fresh on the relay server's clock (the engine estimates it from the token endpoint's
   issued_at, see mcp_server._RelayToken, so this computer's own clock does not matter): exp not
   past and iat not ahead by more than CLOCK_SKEW_MS, a window of at most MAX_REQUEST_TTL_MS, and
   not issued before this worker's first token (a restarted worker cannot be fed a request captured
   before it started);
5. has a request_id this worker has not already accepted inside that window (replay cache).

The engine then executes only the parsed `body`; the unsigned top-level fields exist for engines
from before SEC-021 and are ignored. A rejected request gets no response at all and one line on
stderr (never stdout: that is the MCP protocol on stdio, AGENTS.md constraint 5).

Pure and offline: no network, no clock of its own (the caller passes the server time), so
tests/test_relay_signing.py checks the golden vector only (signed bytes, key id, signature); the verifier's
other branches have no test here because the relay is off in 0.2.0. `cryptography` (a dependency of the `relay`
extra) is imported only where a key is parsed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

SIGNATURE_VERSION = 1
SIGNATURE_ALG = "Ed25519"
SIGNING_CONTEXT = "locus-relay-request/v1"
MAX_REQUEST_TTL_MS = 60_000  # the relay signs a 60 s window; a longer one is refused
CLOCK_SKEW_MS = 30_000  # tolerance on the server-clock estimate (NTP steps, the token's whole-second iat)
NOT_BEFORE_SLOP_MS = 2_000  # a request may be dated this much before the worker's first token
MAX_BODY_CHARS = 256 * 1024  # the web caps the whole JSON-RPC body at 64 KB
# An accepted id is kept this long past the point where check() refuses its request as expired. The
# server-clock estimate can step back when a refreshed token arrives (whole-second issued_at, network
# latency, a wall-clock correction); without the margin, an id forgotten just before such a step
# could be accepted again.
REPLAY_GRACE_MS = 30_000
# Entries; only verified requests get in, and the relay rate limit (60/min) keeps a window's worth far
# below this. When it is full of ids still inside their window, new requests are refused rather than
# a live id evicted (an evicted id could be replayed).
REPLAY_CACHE_MAX = 10_000
MAX_VERIFY_KEYS = 5
VERIFY_KEYS_ENV = "LOCUS_RELAY_VERIFY_KEYS"

_KID_RE = re.compile(r"[0-9a-f]{16}")
_B64_32_RE = re.compile(r"[A-Za-z0-9+/_-]{43}=?")
_B64_SIG_RE = re.compile(r"[A-Za-z0-9+/_-]{86}(==)?")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")


@dataclass(frozen=True)
class VerifyKey:
    kid: str
    key: Any  # cryptography Ed25519PublicKey


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    request: dict[str, Any] | None = None  # what to execute: the signed body plus request_id


def key_id(raw_public_key: bytes) -> str:
    """First 16 hex chars of sha256(raw 32-byte public key) — the same kid the relay computes."""
    return hashlib.sha256(raw_public_key).hexdigest()[:16]


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + "=" * (-len(text) % 4))


def parse_public_key(text: str) -> VerifyKey:
    """A raw 32-byte Ed25519 public key in base64url or base64 (a JWK's "x"). Raises ValueError."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    cleaned = text.strip()
    if not _B64_32_RE.fullmatch(cleaned):
        raise ValueError("not a base64 32-byte Ed25519 public key")
    try:
        raw = _b64decode(cleaned.rstrip("="))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not a base64 32-byte Ed25519 public key") from exc
    if len(raw) != 32:
        raise ValueError("not a base64 32-byte Ed25519 public key")
    return VerifyKey(kid=key_id(raw), key=Ed25519PublicKey.from_public_bytes(raw))


def parse_verify_keys_env(value: str | None) -> tuple[VerifyKey, ...]:
    """LOCUS_RELAY_VERIFY_KEYS: comma-separated public keys, or empty. Raises ValueError when set but
    unusable, so a typo in a pin is an error at startup, never a silently unpinned engine."""
    if value is None or not value.strip():
        return ()
    parts = [part.strip() for part in value.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{VERIFY_KEYS_ENV} has an empty entry")
    if len(parts) > MAX_VERIFY_KEYS:
        raise ValueError(f"{VERIFY_KEYS_ENV} lists more than {MAX_VERIFY_KEYS} keys")
    try:
        keys = tuple(parse_public_key(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"{VERIFY_KEYS_ENV}: {exc}") from None
    return keys


def keys_from_token_response(value: Any) -> tuple[VerifyKey, ...]:
    """The `request_signing_keys` of a /api/relay/token answer. Entries that are not a well-formed
    Ed25519 public key are skipped; the kid is recomputed from the key, never taken from the server."""
    if not isinstance(value, list):
        return ()
    keys: list[VerifyKey] = []
    for entry in value[:MAX_VERIFY_KEYS]:
        if not isinstance(entry, dict) or entry.get("alg") != SIGNATURE_ALG:
            continue
        public_key = entry.get("public_key")
        if not isinstance(public_key, str):
            continue
        try:
            key = parse_public_key(public_key)
        except ValueError:
            continue
        if all(existing.kid != key.kid for existing in keys):
            keys.append(key)
    return tuple(keys)


def signing_message(*, kid: str, user_id: str, request_id: str, iat: int, exp: int, body: str) -> bytes:
    """The exact bytes the relay signs (locus-web/lib/relay-signing.ts relaySigningMessage)."""
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return "\n".join([SIGNING_CONTEXT, kid, user_id, request_id, str(iat), str(exp), body_sha256]).encode("utf-8")


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


class RelayRequestVerifier:
    """Decides, per `mcp_request` payload, whether this worker may execute it (see the module doc).

    `user_id` is this account's (from the token endpoint), `not_before_ms` the server time of this
    worker's first token minus NOT_BEFORE_SLOP_MS. With `pinned` keys, set_keys (token refreshes)
    changes nothing.
    """

    def __init__(self, *, user_id: str, keys: tuple[VerifyKey, ...], not_before_ms: int, pinned: bool = False) -> None:
        self.user_id = user_id
        self.not_before_ms = not_before_ms
        self.pinned = pinned
        self._keys: dict[str, VerifyKey] = {}
        self._seen: OrderedDict[str, int] = OrderedDict()  # request_id -> forget after (server ms)
        self._install(keys)

    def _install(self, keys: tuple[VerifyKey, ...]) -> None:
        self._keys = {key.kid: key for key in keys}

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def set_keys(self, keys: tuple[VerifyKey, ...]) -> None:
        """New keys from a token refresh (a rotation). An empty list is taken as it is: the worker
        then runs nothing until keys come back, rather than keep trusting a key the server dropped."""
        if not self.pinned:
            self._install(keys)

    def check(self, payload: Any, *, now_ms: int) -> Verdict:
        if not isinstance(payload, dict):
            return Verdict(False, "malformed request")
        signed = payload.get("signed")
        if signed is None:
            return Verdict(False, "unsigned")
        if not isinstance(signed, dict):
            return Verdict(False, "malformed signature block")
        if signed.get("v") != SIGNATURE_VERSION or signed.get("alg") != SIGNATURE_ALG:
            return Verdict(False, "unsupported signature version or algorithm")
        request_id = payload.get("request_id")
        kid, iat, exp = signed.get("kid"), _int(signed.get("iat")), _int(signed.get("exp"))
        body, sig = signed.get("body"), signed.get("sig")
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            return Verdict(False, "malformed request_id")
        if not isinstance(kid, str) or not _KID_RE.fullmatch(kid) or iat is None or exp is None:
            return Verdict(False, "malformed signature block")
        if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
            return Verdict(False, "malformed or oversized body")
        if not isinstance(sig, str) or not _B64_SIG_RE.fullmatch(sig):
            return Verdict(False, "malformed signature")
        key = self._keys.get(kid)
        if key is None:
            return Verdict(False, "signed with a key this computer does not trust")
        try:
            message = signing_message(kid=kid, user_id=self.user_id, request_id=request_id, iat=iat, exp=exp, body=body)
        except UnicodeEncodeError:
            # a lone surrogate ("\ud800" in the wire JSON) has no UTF-8 form; the relay never sends
            # one (JSON.stringify escapes it), so it can only come from a direct publish
            return Verdict(False, "malformed or oversized body")
        if not _verify(key, _b64decode(sig.rstrip("=")), message):
            return Verdict(False, "bad signature (altered, or signed for another account)")
        if not 0 < exp - iat <= MAX_REQUEST_TTL_MS:
            return Verdict(False, "validity window too long")
        if now_ms > exp + CLOCK_SKEW_MS:
            return Verdict(False, "expired")
        if iat > now_ms + CLOCK_SKEW_MS:
            return Verdict(False, "issued in the future")
        if iat < self.not_before_ms:
            return Verdict(False, "issued before this computer connected")
        self._forget_expired(now_ms)
        if request_id in self._seen:
            return Verdict(False, "replayed")
        try:
            request = json.loads(body)
        except ValueError:
            return Verdict(False, "body is not JSON")
        if not isinstance(request, dict):
            return Verdict(False, "body is not a JSON object")
        if len(self._seen) >= REPLAY_CACHE_MAX:
            return Verdict(False, "replay cache full")
        self._seen[request_id] = exp + CLOCK_SKEW_MS + REPLAY_GRACE_MS
        return Verdict(True, request={**request, "request_id": request_id})

    def _forget_expired(self, now_ms: int) -> None:
        # Entries past their window (plus REPLAY_GRACE_MS) can go: check() refuses those requests as
        # expired.
        stale = [rid for rid, until in self._seen.items() if until < now_ms]
        for rid in stale:
            del self._seen[rid]


def _verify(key: VerifyKey, signature: bytes, message: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature

    try:
        key.key.verify(signature, message)
    except InvalidSignature:
        return False
    return True
