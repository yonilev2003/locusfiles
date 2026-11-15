"""The public API key `locus serve --transport relay` connects to Locus's Realtime service with.

Not a secret. It is the project's public key (the legacy "anon" JWT or an `sb_publishable_...` key),
the same value Locus's website already ships to every browser; what a holder can do is decided by the
relay's per-account row-level-security policies and the short-lived JWT /api/relay/token mints,
never by keeping this key hidden.

Where the engine gets it, in order (mcp_server._run_relay_async):

1. `LOCUS_MCP_RELAY_SUPABASE_ANON_KEY`, when set. It always wins, so a staging relay or a user who
   wants to pin the value keeps working exactly as before. It is used as given, never checked here.
2. The `realtime_public_key` field of the /api/relay/token answer (engine 0.1.3 on). Nothing else
   is taken from that answer for this purpose: the Realtime URL still comes from the engine's own
   settings, so a token endpoint cannot redirect the connection. The endpoint is already trusted
   with the API key and with the JWT and request-signing keys it hands back (SEC-021, where
   LOCUS_RELAY_VERIFY_KEYS still pins the signing keys). A public key it supplies can at worst make
   the connection fail, and parse_relay_public_key() below refuses anything that is not shaped
   like one before it reaches the websocket URL.

The key is never logged, printed or put in an error message.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

PUBLIC_KEY_ENV = "LOCUS_MCP_RELAY_SUPABASE_ANON_KEY"
PUBLIC_KEY_FIELD = "realtime_public_key"  # of the /api/relay/token answer (the website's relay-public-key module)

# Keep these two patterns identical to the website's relay-public-key module (no test in this repository compares
# them; the relay is off in 0.2.0): the site publishes only what this engine accepts.
MAX_KEY_CHARS = 2048
JWT_PART_PATTERN = r"[A-Za-z0-9_-]{1,2048}"
PUBLISHABLE_PATTERN = r"sb_publishable_[A-Za-z0-9_-]{16,128}"

_JWT_RE = re.compile(rf"({JWT_PART_PATTERN})\.({JWT_PART_PATTERN})\.({JWT_PART_PATTERN})")
_PUBLISHABLE_RE = re.compile(PUBLISHABLE_PATTERN)


def _json_part(part: str) -> Any:
    try:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (binascii.Error, ValueError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None


def parse_relay_public_key(value: Any) -> str | None:
    """*value* when it has the shape of a public Supabase key, else None.

    Either an `sb_publishable_...` key, or a JWT: three dot-separated base64url parts whose first
    two are JSON objects. A JWT that names a role other than `anon` is refused, so a service or
    other privileged key that ended up in the wrong place is never accepted as "public".
    """
    if not isinstance(value, str):
        return None
    key = value.strip()
    if not key or len(key) > MAX_KEY_CHARS:
        return None
    if _PUBLISHABLE_RE.fullmatch(key):
        return key
    match = _JWT_RE.fullmatch(key)
    if not match:
        return None
    header, claims = _json_part(match.group(1)), _json_part(match.group(2))
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return None
    if "role" in claims and claims["role"] != "anon":
        return None
    return key
