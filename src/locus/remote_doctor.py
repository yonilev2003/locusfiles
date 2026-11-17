"""`locus doctor --remote <url>`: check, from this computer, that a Locus server behind a tunnel answers the way an
assistant's cloud needs it to. Each check prints one OK / FAIL line with the fix. Standard library only; no redirect
is followed (a token must never travel to another host), and the token is used in one header and printed nowhere.

The checks run in this order, and a failed one does not stop the later ones that can still run:

1. the host name resolves (DNS);
2. the TLS handshake works and the certificate is trusted for that name (https only);
3. the port is 443 (https only: Claude's cloud connects on 443 only);
4. a GET without a token returns 401, not 200 (open) and not 421 (the host name is not allowed) or 403 (a firewall);
5. POST `initialize` with the token returns 200 and a JSON body (not Server-Sent Events, not 421, not 401);
6. `tools/list` shows only the expected tool names, each marked read-only.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import __version__
from .remote_guard import REMOTE_TOOLS

REQUIRED_TOOLS = ("locus_search", "locus_list_sources", "locus_read_file")
_PROTOCOL = "2025-06-18"


@dataclass
class _Probe:
    ok: bool
    status: int | None = None
    content_type: str = ""
    headers: dict[str, str] | None = None
    body: bytes = b""
    error: str = ""


class RemoteDoctor:
    """The checks for one URL. `timeout` is per request, in seconds."""

    def __init__(
        self, url: str, token: str | None, *, timeout: float = 15.0, out: Callable[[str], None] = print
    ) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self.out = out
        self.failed = False
        self._parts = urlsplit(url)
        self.https = self._parts.scheme == "https"
        self.host = self._parts.hostname or ""
        try:
            self.port = self._parts.port or (443 if self.https else 80)
        except ValueError:
            self.port = 0
        self.path = self._parts.path or "/"
        self._session: str | None = None

    # --- output ---------------------------------------------------------------------------------------------------

    def _line(self, mark: str, label: str, detail: str = "") -> None:
        self.out(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))

    def ok(self, label: str, detail: str = "") -> None:
        self._line("OK  ", label, detail)

    def warn(self, label: str, detail: str = "") -> None:
        self._line("WARN", label, detail)

    def fail(self, label: str, detail: str = "") -> None:
        self.failed = True
        self._line("FAIL", label, detail)

    # --- one request ----------------------------------------------------------------------------------------------

    def _request(self, method: str, headers: dict[str, str], body: bytes | None = None) -> _Probe:
        try:
            if self.https:
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    self.host, self.port, timeout=self.timeout, context=ssl.create_default_context()
                )
            else:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            try:
                conn.request(method, self.path + (f"?{self._parts.query}" if self._parts.query else ""), body, headers)
                response = conn.getresponse()
                data = response.read()
                return _Probe(
                    True,
                    response.status,
                    response.getheader("Content-Type") or "",
                    {k.lower(): v for k, v in response.getheaders()},
                    data,
                )
            finally:
                conn.close()
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            return _Probe(False, error=type(exc).__name__)

    def _rpc(self, payload: dict, session: str | None = None) -> _Probe:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        return self._request("POST", headers, json.dumps(payload).encode())

    # --- the checks -----------------------------------------------------------------------------------------------

    def run(self) -> bool:
        """Run every check; True when none failed."""
        if self._parts.scheme not in ("http", "https") or not self.host or not self.port:
            self.fail("address", "that is not a web address. Use the full address, e.g. https://abc.ts.net/mcp")
            return False
        if self._parts.username or self._parts.password or self._parts.query:
            self.fail(
                "address",
                "the address must not carry a user name, a password or a query: put the token in "
                "LOCUS_MCP_TOKEN (or --token-env), never in the URL.",
            )
            return False
        self._dns()
        self._tls()
        self._port()
        self._unauthenticated_get()
        self._initialize_and_tools()
        return not self.failed

    def _dns(self) -> None:
        try:
            socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        except OSError:
            self.fail(
                "DNS",
                f"{self.host} does not resolve. Copy the address the tunnel printed again; a Cloudflare quick "
                "tunnel gets a new one at every start.",
            )
            return
        self.ok("DNS", f"{self.host} resolves")

    def _tls(self) -> None:
        if not self.https:
            self.warn("TLS", "plain http: fine for a test on this computer; Claude and ChatGPT need an https address")
            return
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as raw:
                with ssl.create_default_context().wrap_socket(raw, server_hostname=self.host):
                    pass
        except ssl.SSLCertVerificationError:
            self.fail("TLS", "the certificate is not trusted for this name. Use the https address the tunnel gave you.")
        except (OSError, ssl.SSLError) as exc:
            self.fail("TLS", f"no secure connection ({type(exc).__name__}). Is the tunnel running?")
        else:
            self.ok("TLS", "secure connection, certificate trusted")

    def _port(self) -> None:
        if not self.https:
            return
        if self.port == 443:
            self.ok("port 443", "Claude's cloud connects on 443 only")
        else:
            self.fail(
                "port 443",
                f"the address uses port {self.port}; Claude connects on 443 only. Use the tunnel's "
                "https address without a port.",
            )

    def _unauthenticated_get(self) -> None:
        got = self._request("GET", {"Accept": "text/event-stream"})
        label = "no token -> 401"
        if not got.ok:
            self.fail(
                label,
                f"no answer ({got.error}). Is the tunnel up and `locus serve --transport streamable-http` running?",
            )
        elif got.status == 401:
            self.ok(label, "the server refuses a request without the token")
        elif got.status == 200:
            self.fail(label, "SECURITY: the server answered 200 without a token. Stop it and check LOCUS_MCP_TOKEN.")
        else:
            self.fail(label, self._explain(got))

    @staticmethod
    def _explain(got: _Probe) -> str:
        status = got.status
        if status == 421:
            return (
                "421: the server does not accept this host name. Start it with "
                "LOCUS_MCP_PUBLIC_HOST=<the tunnel's host name> (or --public-host)."
            )
        if status == 403:
            return (
                "403: something in front of Locus (a firewall, a Cloudflare Access or WAF rule) is blocking the "
                "request. Claude's and ChatGPT's servers would be blocked too."
            )
        if status == 404:
            return "404: no MCP endpoint at this path. The default path is /mcp."
        if status == 503:
            return (
                "503: the REMOTE_OFF kill-switch file exists on the computer that runs Locus. "
                "Delete it to turn remote access on."
            )
        if status == 429:
            return "429: rate limit or too many failed attempts. Wait a minute and run the check again."
        if status in (502, 504, 530):
            return (
                f"{status}: the tunnel is up but Locus does not answer. Start `locus serve --transport streamable-http`"
            )
        return f"unexpected status {status}."

    def _initialize_and_tools(self) -> None:
        if not self.token:
            for label in ("initialize with the token", "tools/list"):
                self.fail(label, "no token to try. Set LOCUS_MCP_TOKEN, or name the variable with --token-env NAME.")
            return
        if self._parts.scheme == "http" and self.host not in ("127.0.0.1", "localhost", "::1"):
            self.warn("token", "sent over plain http to a non-local host")
        init = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "locus-doctor", "version": __version__},
                },
            }
        )
        label = "initialize with the token"
        if not init.ok:
            self.fail(label, f"no answer ({init.error}).")
            self.fail("tools/list", "skipped: initialize failed.")
            return
        if init.status == 401:
            self.fail(
                label,
                "401: the token is not the one the server holds, or the session expired (--expires). "
                "Check LOCUS_MCP_TOKEN and restart `locus serve` if it ran longer than its expiry.",
            )
        elif init.status != 200:
            self.fail(label, self._explain(init))
        elif init.content_type.lower().startswith("text/event-stream"):
            self.fail(
                label,
                "the reply is a Server-Sent Events stream; Cloudflare quick tunnels cannot carry it. "
                "Start `locus serve` without --sse (JSON replies are the default with a public host).",
            )
        elif not init.content_type.lower().startswith("application/json") or not self._is_json_rpc_result(init.body):
            self.fail(label, f"200, but not a JSON reply ({init.content_type or 'no content type'}).")
        else:
            self.ok(label, "200, JSON reply")
            self._session = (init.headers or {}).get("mcp-session-id")
            self._tools()
            return
        self.fail("tools/list", "skipped: initialize failed.")

    @staticmethod
    def _is_json_rpc_result(body: bytes) -> bool:
        try:
            obj = json.loads(body)
        except ValueError:
            return False
        return isinstance(obj, dict) and isinstance(obj.get("result"), dict)

    def _tools(self) -> None:
        if self._session:  # a server that keeps sessions wants the handshake finished first
            self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, self._session)
        got = self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, self._session)
        label = "tools/list"
        if not got.ok or got.status != 200:
            self.fail(
                label, "the tool list could not be read" + (f" ({self._explain(got)})" if got.ok else f" ({got.error})")
            )
            return
        try:
            tools = json.loads(got.body)["result"]["tools"]
            names = [t["name"] for t in tools]
        except (ValueError, KeyError, TypeError):
            self.fail(label, "the reply is not a tool list.")
            return
        problems: list[str] = []
        if missing := [n for n in REQUIRED_TOOLS if n not in names]:
            problems.append(f"missing {', '.join(missing)}")
        if extra := [n for n in names if n not in REMOTE_TOOLS]:
            problems.append(
                f"{', '.join(extra)} should not be reachable through a tunnel (start `locus serve` without --all-tools)"
            )
        not_read_only = [
            t["name"]
            for t in tools
            if not (isinstance(t.get("annotations"), dict) and t["annotations"].get("readOnlyHint") is True)
        ]
        if not_read_only:
            problems.append(f"not marked read-only: {', '.join(not_read_only)}")
        if problems:
            self.fail(label, "; ".join(problems))
        else:
            self.ok(label, f"{len(names)} tools, all read-only: {', '.join(sorted(names))}")


def run_remote_doctor(url: str, token: str | None, *, timeout: float = 15.0, out: Callable[[str], None] = print) -> int:
    """Print the checks for `url`; the exit code is 0 when all passed and 1 when any failed."""
    doctor = RemoteDoctor(url, token, timeout=timeout, out=out)
    passed = doctor.run()
    out(
        "\nAll remote checks passed."
        if passed
        else "\nFix the FAIL lines above, then run `locus doctor --remote` again."
    )
    return 0 if passed else 1
