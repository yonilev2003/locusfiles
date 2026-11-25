"""Helpers for the tests of the tunnel (public-host) mode: the real app on a real local port in a thread, a small HTTP
caller built on the standard library, and a guard that puts the process-wide MCP server back as it was."""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn

from locus import mcp_server as srv

TOKEN = "Tq7-remote-test-token-0123456789abcdef-XYZ"  # 40 characters, like a real one
assert len(TOKEN) >= 32


@contextmanager
def preserved_server_state() -> Iterator[None]:
    """`build_remote_app` changes the tool list and the instructions of the one MCP server in this process; the other
    tests must see it as it was."""
    manager = srv.mcp._tool_manager
    tools = dict(manager._tools)
    instructions = srv.mcp._lowlevel_server.instructions
    check_enabled = srv._check_enabled
    try:
        yield
    finally:
        manager._tools.clear()
        manager._tools.update(tools)
        srv.mcp._lowlevel_server.instructions = instructions
        srv._check_enabled = check_enabled


@contextmanager
def serve_app(app: Any) -> Iterator[int]:
    """Run an ASGI app on 127.0.0.1 with a free port; yields the port."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "the test server did not start"
    try:
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(15)


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body)


def http_call(
    port: int,
    method: str = "GET",
    path: str = "/mcp",
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> Reply:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
    finally:
        conn.close()


def rpc(
    port: int,
    payload: dict[str, Any],
    *,
    token: str | None = TOKEN,
    host: str | None = "example.test",
    extra: dict[str, str] | None = None,
) -> Reply:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if host is not None:
        headers["Host"] = host
    headers.update(extra or {})
    return http_call(port, "POST", "/mcp", headers=headers, body=json.dumps(payload).encode())


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def call_tool(port: int, name: str, arguments: dict[str, Any], **kw: Any) -> Reply:
    return rpc(
        port,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        **kw,
    )
