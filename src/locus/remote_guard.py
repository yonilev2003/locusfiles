"""The safety floor for `locus serve --transport streamable-http` when a user-run tunnel makes it reachable
from the internet ("public-host mode", switched on by `LOCUS_MCP_PUBLIC_HOST` or `--public-host`).

Everything here is plain Python plus one lazy import of the MCP SDK's Host/Origin settings; it imports nothing
from `api.py`, `service.py` or `mcp_server.py`, so each piece can be tested alone and reused (a later OAuth layer
replaces `static_token_authenticator` and leaves the rest as it is).

What public-host mode adds on top of the loopback mode (which is unchanged):

- the public hostname is let through the SDK's DNS-rebinding guard; any other Host gets 421 (`transport_security`);
- the token must be at least 32 characters (`check_token_strength`);
- per-token sliding-window rate limit (default 120 a minute, `LOCUS_MCP_RATE_PER_MIN`), 429 + Retry-After;
- failed-authentication throttle: 10 failures a minute from one client address, then 429;
- a counts-only audit log, one JSON line per request under `<data dir>/audit/` (mode 0600): time, HTTP method,
  MCP method, tool name, HTTP status, number of results, bytes returned and the indexed paths returned. Never the
  query, never a chunk or file text, never the token;
- a kill-switch file `REMOTE_OFF` (in the data dir, and in `~/.locus` when that is another folder): while it
  exists every request gets 503, checked on every request;
- an expiry (`--expires 8h`, 0 turns it off): after it every request gets 401, so a forgotten tunnel goes quiet.

The token is never printed, logged or put in an error message anywhere in this module.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PUBLIC_HOST_ENV = "LOCUS_MCP_PUBLIC_HOST"
RATE_ENV = "LOCUS_MCP_RATE_PER_MIN"
CHATGPT_TOOLS_ENV = "LOCUS_CHATGPT_TOOLS"

MIN_TOKEN_CHARS = 32
DEFAULT_RATE_PER_MIN = 120
FAILED_AUTH_PER_MIN = 10
DEFAULT_EXPIRES = "8h"
KILL_SWITCH_FILE = "REMOTE_OFF"
AUDIT_DIR = "audit"

# The tools a tunnel may reach: read-only, over the index. `search` and `fetch` are the ChatGPT-shaped aliases
# (remote_tools.py). The two others describe this computer's setup or contact Locus's servers: not for a tunnel.
REMOTE_TOOLS: tuple[str, ...] = (
    "locus_search",
    "locus_list_sources",
    "locus_read_file",
    "locus_help",
    "search",
    "fetch",
)
LOCAL_ONLY_TOOLS: tuple[str, ...] = ("locus_diagnose_connection", "locus_account_status")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_OFF = frozenset({"0", "false", "no", "off"})
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^{_HOST_LABEL}(?:\.{_HOST_LABEL})*$")

# Read at most this much of a request or response body to fill the audit line; bigger bodies are passed on
# untouched and their line says "results": null.
AUDIT_BODY_CAP = 1_048_576
MAX_AUDIT_PATHS = 100


class RemoteConfigError(ValueError):
    """A public-host setting that cannot be used. The message is for the person who runs `locus serve`."""


# --- configuration -----------------------------------------------------------------------------------------------


def parse_public_hosts(value: str | None) -> tuple[str, ...]:
    """Hostnames from a comma-separated list (`abc.ts.net, locus.example.com`). A pasted URL is reduced to its host
    and a port is dropped. Exact names only: no wildcards, no IP addresses with a port, nothing but letters, digits,
    dots and hyphens. An empty or missing value means no public host (loopback mode)."""
    hosts: list[str] = []
    for raw in (value or "").split(","):
        item = raw.strip().lower()
        if not item:
            continue
        item = re.sub(r"^[a-z][a-z0-9+.-]*://", "", item)
        item = item.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "@" in item:
            raise RemoteConfigError(f"{PUBLIC_HOST_ENV}: a host name only, without a user name or password.")
        item = re.sub(r":\d+$", "", item)
        if "*" in item or not _HOST_RE.match(item) or len(item) > 253:
            raise RemoteConfigError(
                f"{PUBLIC_HOST_ENV}: {item!r} is not a plain host name. Give the exact name the tunnel prints, "
                "for example abc-def.trycloudflare.com (no wildcards)."
            )
        if item not in hosts:
            hosts.append(item)
    return tuple(hosts)


def transport_security(public_hosts: Iterable[str]) -> Any:
    """The SDK's Host/Origin settings for public-host mode: DNS-rebinding protection stays ON; the localhost forms
    and the public hostnames are allowed (and `https://<host>` as an Origin); anything else is a 421 (Host) or 403
    (Origin). A request with no Origin header passes: servers that call from their cloud send none."""
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for host in public_hosts:
        hosts += [host, f"{host}:*"]
        origins.append(f"https://{host}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def check_token_strength(token: str) -> str | None:
    """None when the token is long enough for a tunnel, else the reason (never the token itself)."""
    if len(token) >= MIN_TOKEN_CHARS:
        return None
    return (
        f"LOCUS_MCP_TOKEN is {len(token)} characters long; a server reachable through a tunnel needs at least "
        f"{MIN_TOKEN_CHARS}. Make a new one and put it in .env:\n"
        '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
    )


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text: str) -> float | None:
    """`8h`, `30m`, `90s`, `2d` as seconds; `0` is None (no expiry). A number without a unit is refused (hours or
    seconds? say which)."""
    match = _DURATION_RE.match(text or "")
    if not match:
        raise RemoteConfigError(
            f"--expires {text!r}: write a number and a unit, for example 8h, 30m or 2d (0 = never)."
        )
    number, unit = float(match.group(1)), match.group(2).lower()
    if number == 0:
        return None
    if not unit:
        raise RemoteConfigError(f"--expires {text!r}: add a unit, for example {text}h or {text}m (0 = never).")
    return number * _UNIT_SECONDS[unit]


@dataclass(frozen=True)
class RemoteOptions:
    """Everything public-host mode needs. Built by `build_options`."""

    public_hosts: tuple[str, ...]
    data_dir: Path
    sse: bool = False
    all_tools: bool = False
    expires_s: float | None = 8 * 3600.0
    rate_per_min: int = DEFAULT_RATE_PER_MIN
    chatgpt_tools: bool = True


def build_options(
    *,
    host: str,
    data_dir: Path,
    public_host: str | None = None,
    sse: bool = False,
    all_tools: bool = False,
    expires: str | None = None,
    env: dict[str, str] | None = None,
) -> RemoteOptions | None:
    """None when no public host is set (loopback mode, nothing changes). Raises `RemoteConfigError` for a setting
    that cannot be used, including a bind address other than loopback: a tunnel client runs on this computer and
    reaches 127.0.0.1, so there is no reason to listen on the network."""
    values = os.environ if env is None else env
    hosts = parse_public_hosts(public_host if public_host else values.get(PUBLIC_HOST_ENV))
    if not hosts:
        return None
    if host not in LOOPBACK_HOSTS:
        raise RemoteConfigError(
            f"With a public host, Locus listens on this computer only (127.0.0.1); --host {host} is refused. The "
            "tunnel program runs here and connects to 127.0.0.1."
        )
    rate_text = (values.get(RATE_ENV) or "").strip()
    rate = DEFAULT_RATE_PER_MIN
    if rate_text:
        if not rate_text.isdigit() or int(rate_text) < 1:
            raise RemoteConfigError(f"{RATE_ENV}: a whole number of requests per minute, 1 or more.")
        rate = int(rate_text)
    return RemoteOptions(
        public_hosts=hosts,
        data_dir=data_dir,
        sse=sse,
        all_tools=all_tools,
        expires_s=parse_duration(expires if expires is not None else DEFAULT_EXPIRES),
        rate_per_min=rate,
        chatgpt_tools=(values.get(CHATGPT_TOOLS_ENV) or "1").strip().lower() not in _OFF,
    )


# --- authentication (the one place a token is checked) -------------------------------------------------------------

Authenticator = Callable[[str], bool]


def static_token_authenticator(token: str) -> Authenticator:
    """A check for one fixed bearer token, compared in constant time on bytes (so a non-ASCII header value is a
    plain 'no', never an error). An OAuth access-token check can replace this function: the guard only calls
    `authenticate(supplied_bearer_string)`."""
    expected = token.encode("utf-8")

    def authenticate(supplied: str) -> bool:
        return hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"), expected)

    return authenticate


def bearer_from_headers(headers: Iterable[tuple[bytes, bytes]]) -> str:
    """The text after `Bearer ` in the Authorization header, or '' when there is none."""
    for name, value in headers:
        if name.lower() == b"authorization":
            text = value.decode("latin-1")
            return text[len("Bearer ") :] if text.startswith("Bearer ") else ""
    return ""


def client_key(scope: dict[str, Any]) -> str:
    """Who is asking, for the failed-authentication throttle. Behind a tunnel every request arrives from the tunnel
    program on this computer, so when the peer is loopback the LAST address of X-Forwarded-For (the one the tunnel's
    edge appended) is used; from any other peer the header is not trusted. Not verified against each tunnel product."""
    peer = (scope.get("client") or ("unknown", 0))[0]
    if peer in ("127.0.0.1", "::1", "localhost"):
        for name, value in scope.get("headers") or []:
            if name.lower() == b"x-forwarded-for":
                last = value.decode("latin-1").split(",")[-1].strip()
                try:
                    return str(ipaddress.ip_address(last))
                except ValueError:
                    break
    return str(peer)


# --- limits ------------------------------------------------------------------------------------------------------


class SlidingWindowLimiter:
    """At most `limit` events per key in the last `window` seconds. `retry_after(key)` is 0.0 when another event
    fits, else the seconds until the oldest one leaves the window; `record(key)` counts an event."""

    def __init__(
        self, limit: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self.window:
            events.popleft()
        return events

    def retry_after(self, key: str) -> float:
        with self._lock:
            now = self._clock()
            events = self._trim(key, now)
            if len(events) < self.limit:
                if not events:
                    self._events.pop(key, None)
                return 0.0
            return max(0.001, self.window - (now - events[0]))

    def record(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._events) >= self._max_keys and key not in self._events:
                for stale in [k for k, v in self._events.items() if not v or now - v[-1] >= self.window]:
                    del self._events[stale]
                if len(self._events) >= self._max_keys:  # a flood of new keys: forget the oldest half
                    for old in list(self._events)[: self._max_keys // 2]:
                        del self._events[old]
            self._trim(key, now).append(now)


def kill_switch_paths(data_dir: Path) -> list[Path]:
    """Where a `REMOTE_OFF` file counts: in the data folder, and in `~/.locus` when the data folder is elsewhere."""
    paths = [Path(data_dir) / KILL_SWITCH_FILE]
    home_file = Path.home() / ".locus" / KILL_SWITCH_FILE
    if home_file not in paths:
        paths.append(home_file)
    return paths


def kill_switch_active(data_dir: Path) -> bool:
    try:
        return any(p.exists() for p in kill_switch_paths(data_dir))
    except OSError:
        return False


# --- audit log: counts only ----------------------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9_./:-]{1,64}$")
_SEARCH_HEADER = re.compile(r"^## \d+\. (?P<path>.+?)  \(chunk \d+", re.M)
# A collapsed row lists the other copies of its text: "also in: /a (chunk 0), /b (chunk 2), and 3 more".
_ALSO_IN = re.compile(r"^also in: (?P<list>.+)$", re.M)
_ALSO_IN_ENTRY = re.compile(r"(?:^|, )(?P<path>.+?) \(chunk \d+\)")


@dataclass(frozen=True)
class RequestInfo:
    method: str | None = None  # the JSON-RPC method, e.g. tools/call
    tool: str | None = None  # the tool name of a tools/call
    path_argument: str | None = None  # the `path` of a locus_read_file call (an indexed path, never a query)


def summarize_request(body: bytes) -> RequestInfo:
    """The method and tool name of a JSON-RPC request. Arguments other than a read's `path` are never looked at."""
    try:
        obj = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return RequestInfo()
    if isinstance(obj, list):
        return RequestInfo(method="batch")
    if not isinstance(obj, dict):
        return RequestInfo()
    method = obj.get("method")
    method = method if isinstance(method, str) and _NAME_RE.match(method) else None
    tool = path_argument = None
    if method == "tools/call" and isinstance(obj.get("params"), dict):
        params = obj["params"]
        name = params.get("name")
        tool = name if isinstance(name, str) and _NAME_RE.match(name) else "(invalid)"
        args = params.get("arguments")
        if tool == "locus_read_file" and isinstance(args, dict) and isinstance(args.get("path"), str):
            path_argument = args["path"]
    return RequestInfo(method=method, tool=tool, path_argument=path_argument)


def _json_payloads(body: bytes, content_type: str) -> list[dict[str, Any]]:
    text = body.decode("utf-8", "replace")
    chunks = (
        [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if content_type.lower().startswith("text/event-stream")
        else [text]
    )
    out: list[dict[str, Any]] = []
    for chunk in chunks:
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def summarize_response(
    info: RequestInfo, body: bytes, content_type: str, known_paths: Callable[[], set[str]] | None
) -> tuple[int | None, list[str]]:
    """(number of results, paths) of a tools/call answer, or (None, []) when it cannot be told. A path is logged only
    when it is one of the INDEXED paths (`known_paths`), so text inside a chunk that imitates a result header can never
    reach the log. The query and the chunk text are not read here."""
    if info.method != "tools/call" or info.tool is None:
        return None, []
    result: dict[str, Any] | None = None
    for payload in _json_payloads(body, content_type):
        if isinstance(payload.get("result"), dict):
            result = payload["result"]
    if result is None:
        return None, []
    if result.get("isError"):
        return 0, []
    structured = result.get("structuredContent")
    if info.tool == "search" and isinstance(structured, dict) and isinstance(structured.get("results"), list):
        ids = [r.get("id") for r in structured["results"] if isinstance(r, dict) and isinstance(r.get("id"), str)]
        return len(structured["results"]), _unique(i.rpartition("#")[0] for i in ids)
    if info.tool == "fetch" and isinstance(structured, dict) and isinstance(structured.get("metadata"), dict):
        path = structured["metadata"].get("path")
        return (1, [path]) if isinstance(path, str) else (1, [])
    text = "\n".join(
        part["text"]
        for part in result.get("content") or []
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
    )
    if info.tool not in ("locus_search", "locus_list_sources", "locus_read_file"):
        return None, []
    try:
        known = known_paths() if known_paths else set()
    except Exception:  # noqa: BLE001 - the log is best effort
        known = set()
    if not known:
        return None, []
    if info.tool == "locus_search":
        found = [m.group("path") for m in _SEARCH_HEADER.finditer(text)]
        shown = [
            e.group("path") for line in _ALSO_IN.finditer(text) for e in _ALSO_IN_ENTRY.finditer(line.group("list"))
        ]
        rows = [p for p in found if p in known]
        return len(rows), _unique([*rows, *(p for p in shown if p in known)])  # every path the answer showed
    elif info.tool == "locus_list_sources":
        found = [line[2:] for line in text.splitlines() if line.startswith("- ")]
    else:  # locus_read_file: the path the caller gave, when the answer is not an error text
        path = info.path_argument
        if path is None or text.startswith("Error:"):
            return 0, []
        if path not in known:
            by_nfc = {unicodedata.normalize("NFC", p): p for p in known}
            path = by_nfc.get(unicodedata.normalize("NFC", path), path)
        found = [path]
    kept = [p for p in found if p in known]
    return len(kept), _unique(kept)


def _unique(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item:
            seen.setdefault(item, None)
    return list(seen)


class AuditLog:
    """One JSON line per request, appended to `<data dir>/audit/remote-YYYY-MM-DD.jsonl` (folder 0700, file 0600).
    Writing never raises: a log that cannot be written is reported once on stderr and the server goes on."""

    def __init__(self, data_dir: Path, wall_clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(data_dir) / AUDIT_DIR
        self._wall = wall_clock
        self._lock = threading.Lock()
        self._complained = False

    def write(
        self,
        *,
        http: str,
        method: str | None,
        tool: str | None,
        status: int,
        results: int | None,
        bytes_returned: int,
        paths: list[str],
    ) -> None:
        now = datetime.fromtimestamp(self._wall(), UTC)
        line: dict[str, Any] = {
            "ts": now.isoformat(timespec="seconds"),
            "http": http,
            "method": method,
            "tool": tool,
            "status": status,
            "results": results,
            "bytes": bytes_returned,
            "paths": paths[:MAX_AUDIT_PATHS],
        }
        if len(paths) > MAX_AUDIT_PATHS:
            line["paths_total"] = len(paths)
        data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._lock:
                self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                target = self.dir / f"remote-{now:%Y-%m-%d}.jsonl"
                fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, data)
                finally:
                    os.close(fd)
        except OSError as exc:
            if not self._complained:
                self._complained = True
                print(f"locus_mcp: audit log not written ({type(exc).__name__})", file=sys.stderr)


# --- the middleware --------------------------------------------------------------------------------------------------

ASGIApp = Callable[[dict[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]]


async def _respond(send: Any, status: int, text: str, extra: Iterable[tuple[bytes, bytes]] = ()) -> int:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
    return len(body)


def without_event_stream(app: Any) -> Any:
    """ASGI middleware for JSON, stateless replies: a GET answers 405 (`Allow: POST`). Such a server never has
    anything to push, and the SDK would answer a GET with an event stream that stays open until the client leaves, which
    a Cloudflare quick tunnel cannot carry and which would hold a connection for nothing. The MCP transport spec lets a
    server answer a GET with 405. Other methods and lifespan events pass through to the SDK."""

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("method") == "GET":
            await _respond(
                send, 405, "Method Not Allowed: this server answers POST requests only.", [(b"allow", b"POST")]
            )
            return
        await app(scope, receive, send)

    return guarded


class RemoteGuard:
    """ASGI middleware around the MCP app. Order of the checks on every HTTP request: kill switch (503), expiry
    (401), failed-authentication throttle (429), bearer token (401), per-token rate limit (429). Only then is the
    request passed on, and its size, status and result paths are written to the audit log."""

    def __init__(
        self,
        app: Any,
        *,
        authenticate: Authenticator,
        data_dir: Path,
        expires_in: float | None = None,
        rate_per_min: int = DEFAULT_RATE_PER_MIN,
        known_paths: Callable[[], set[str]] | None = None,
        audit: bool = True,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.authenticate = authenticate
        self.data_dir = Path(data_dir)
        self.clock = clock
        self.expires_at = None if expires_in is None else clock() + expires_in
        self.rate = SlidingWindowLimiter(rate_per_min, 60.0, clock)
        self.failures = SlidingWindowLimiter(FAILED_AUTH_PER_MIN, 60.0, clock)
        self.known_paths = known_paths
        self.audit = AuditLog(data_dir, wall_clock) if audit else None

    def _log(self, scope: dict[str, Any], status: int, sent: int, info: RequestInfo | None = None, **more: Any) -> None:
        if self.audit is None:
            return
        info = info or RequestInfo()
        self.audit.write(
            http=str(scope.get("method", "")),
            method=info.method,
            tool=info.tool,
            status=status,
            results=more.get("results"),
            bytes_returned=sent,
            paths=more.get("paths") or [],
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # lifespan and the like
            await self.app(scope, receive, send)
            return
        if kill_switch_active(self.data_dir):
            sent = await _respond(
                send, 503, f"Remote access is switched off: delete the {KILL_SWITCH_FILE} file to turn it on again."
            )
            self._log(scope, 503, sent)
            return
        if self.expires_at is not None and self.clock() >= self.expires_at:
            sent = await _respond(
                send, 401, "This remote session has expired. Restart `locus serve` to open a new one."
            )
            self._log(scope, 401, sent)
            return
        who = client_key(scope)
        wait = self.failures.retry_after(who)
        if wait:
            await _respond(send, 429, "Too many failed attempts. Try again later.", [self._retry_header(wait)])
            return
        supplied = bearer_from_headers(scope.get("headers") or [])
        if not supplied or not self.authenticate(supplied):
            self.failures.record(who)
            sent = await _respond(
                send,
                401,
                "Unauthorized: missing or invalid bearer token",
                [(b"www-authenticate", b'Bearer realm="locus"')],
            )
            self._log(scope, 401, sent)
            return
        token_key = hashlib.sha256(supplied.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        wait = self.rate.retry_after(token_key)
        if wait:
            sent = await _respond(send, 429, "Too many requests. Try again shortly.", [self._retry_header(wait)])
            self._log(scope, 429, sent)
            return
        self.rate.record(token_key)
        await self._forward(scope, receive, send)

    @staticmethod
    def _retry_header(wait: float) -> tuple[bytes, bytes]:
        return b"retry-after", str(max(1, int(wait + 0.999))).encode()

    @staticmethod
    async def _read_body(receive: Any) -> tuple[list[Any], bytearray]:
        """The request messages read so far (stopping once the body passes AUDIT_BODY_CAP), and the body in them."""
        buffered: list[Any] = []
        body = bytearray()
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body") or len(body) > AUDIT_BODY_CAP:
                break
        return buffered, body

    @staticmethod
    def _replayer(buffered: list[Any], receive: Any) -> Any:
        async def replay() -> Any:  # the same messages again, then whatever is still to come
            return buffered.pop(0) if buffered else await receive()

        return replay

    async def _forward(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self.audit is None:
            await self.app(scope, receive, send)
            return
        replay: Any = receive
        info = RequestInfo()
        if scope.get("method") == "POST":
            buffered, body = await self._read_body(receive)
            replay = self._replayer(buffered, receive)
            if len(body) <= AUDIT_BODY_CAP:
                info = summarize_request(bytes(body))
        status, sent, content_type = 500, 0, ""
        captured = bytearray()

        async def capture(message: Any) -> None:
            nonlocal status, sent, content_type
            if message["type"] == "http.response.start":
                status = int(message["status"])
                for name, value in message.get("headers") or []:
                    if name.lower() == b"content-type":
                        content_type = value.decode("latin-1")
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                sent += len(chunk)
                if len(captured) <= AUDIT_BODY_CAP:
                    captured.extend(chunk)
            await send(message)

        try:
            await self.app(scope, replay, capture)
        finally:
            results: int | None = None
            paths: list[str] = []
            if status == 200 and 0 < len(captured) <= AUDIT_BODY_CAP:
                results, paths = summarize_response(info, bytes(captured), content_type, self.known_paths)
            self._log(scope, status, sent, info, results=results, paths=paths)
