"""remote_guard.py: the settings parsers, the limiters, the audit log and the middleware, one piece at a time with a
fake clock and a fake app (no server, no index). The whole thing on a real port is in test_remote_http.py."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from locus import remote_guard as rg

TOKEN = "Tq7-remote-test-token-0123456789abcdef-XYZ"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


# --- settings -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ()),
        ("", ()),
        (" , ", ()),
        ("abc.ts.net", ("abc.ts.net",)),
        ("ABC.ts.net", ("abc.ts.net",)),
        ("https://abc.ts.net/mcp", ("abc.ts.net",)),
        ("abc.ts.net:443", ("abc.ts.net",)),
        ("a.example.test, b.example.test,a.example.test", ("a.example.test", "b.example.test")),
        ("locus", ("locus",)),
    ],
)
def test_public_hosts_are_reduced_to_plain_lowercase_names(value, expected):
    assert rg.parse_public_hosts(value) == expected


@pytest.mark.parametrize("bad", ["*.example.test", "a b.test", "user@host.test", "bad_name.test", "-x.test", "a..b"])
def test_public_hosts_refuse_wildcards_and_odd_names(bad):
    with pytest.raises(rg.RemoteConfigError):
        rg.parse_public_hosts(bad)


def test_the_transport_security_keeps_the_protection_on_and_lists_the_localhost_forms_and_the_public_host():
    security = rg.transport_security(["abc.ts.net"])
    assert security.enable_dns_rebinding_protection is True
    assert {"127.0.0.1:*", "localhost:*", "[::1]:*", "abc.ts.net", "abc.ts.net:*"} <= set(security.allowed_hosts)
    assert "https://abc.ts.net" in security.allowed_origins
    assert "http://127.0.0.1:*" in security.allowed_origins
    assert not any("*" in h and h.startswith("*") for h in security.allowed_hosts)


def test_a_token_shorter_than_32_characters_is_refused_and_never_echoed():
    assert rg.check_token_strength("x" * 32) is None
    short = "SECRETVALUE" + "x" * 20  # 31 characters
    message = rg.check_token_strength(short)
    assert message and "at least 32" in message and "SECRETVALUE" not in message and "31 characters" in message


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("8h", 28800.0), ("30m", 1800.0), ("90s", 90.0), ("2d", 172800.0), ("1.5h", 5400.0), ("0", None), ("0h", None)],
)
def test_durations(text, seconds):
    assert rg.parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "soon", "8", "-1h", "8 hours", "h"])
def test_bad_durations_are_refused(bad):
    with pytest.raises(rg.RemoteConfigError):
        rg.parse_duration(bad)


def test_options_default_to_eight_hours_120_a_minute_and_the_chatgpt_tools_on(tmp_path):
    options = rg.build_options(host="127.0.0.1", data_dir=tmp_path, public_host="abc.ts.net", env={})
    assert (options.expires_s, options.rate_per_min, options.chatgpt_tools) == (28800.0, 120, True)
    assert options.sse is False and options.all_tools is False


def test_no_public_host_means_no_options_so_nothing_changes(tmp_path):
    assert rg.build_options(host="127.0.0.1", data_dir=tmp_path, env={}) is None
    assert rg.build_options(host="0.0.0.0", data_dir=tmp_path, env={}) is None  # not a tunnel setup: untouched


def test_the_environment_variables_are_read(tmp_path):
    env = {"LOCUS_MCP_PUBLIC_HOST": "e.example.test", "LOCUS_MCP_RATE_PER_MIN": "7", "LOCUS_CHATGPT_TOOLS": "off"}
    options = rg.build_options(host="localhost", data_dir=tmp_path, env=env)
    assert (options.public_hosts, options.rate_per_min, options.chatgpt_tools) == (("e.example.test",), 7, False)
    # the flag wins over the environment
    flagged = rg.build_options(host="localhost", data_dir=tmp_path, public_host="f.example.test", env=env)
    assert flagged.public_hosts == ("f.example.test",)


@pytest.mark.parametrize("bad", ["0", "-3", "many", "1.5"])
def test_a_bad_rate_is_refused(tmp_path, bad):
    with pytest.raises(rg.RemoteConfigError):
        rg.build_options(
            host="127.0.0.1", data_dir=tmp_path, env={"LOCUS_MCP_PUBLIC_HOST": "a.test", "LOCUS_MCP_RATE_PER_MIN": bad}
        )


def test_a_public_host_is_refused_on_any_address_but_loopback(tmp_path):
    with pytest.raises(rg.RemoteConfigError, match="127.0.0.1"):
        rg.build_options(host="0.0.0.0", data_dir=tmp_path, public_host="a.test", env={})


# --- the one place a token is checked -------------------------------------------------------------------------------


def test_the_static_authenticator_compares_exactly_and_survives_odd_input():
    check = rg.static_token_authenticator(TOKEN)
    assert check(TOKEN) is True
    assert not any(check(x) for x in ("", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), "h\xe9llo", "\ud800", "é" * 40))


def test_the_bearer_is_read_from_the_authorization_header_only():
    assert rg.bearer_from_headers([(b"Authorization", b"Bearer abc")]) == "abc"
    assert rg.bearer_from_headers([(b"authorization", b"bearer abc")]) == ""
    assert rg.bearer_from_headers([(b"authorization", b"Basic abc")]) == ""
    assert rg.bearer_from_headers([(b"x-api-key", b"abc")]) == ""
    assert rg.bearer_from_headers([]) == ""


def test_the_client_address_is_the_last_forwarded_address_only_behind_a_local_tunnel():
    behind = {"client": ("127.0.0.1", 5), "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")]}
    assert rg.client_key(behind) == "203.0.113.9"
    assert rg.client_key({"client": ("127.0.0.1", 5), "headers": [(b"x-forwarded-for", b"not-an-ip")]}) == "127.0.0.1"
    assert rg.client_key({"client": ("127.0.0.1", 5), "headers": []}) == "127.0.0.1"
    direct = {"client": ("198.51.100.4", 5), "headers": [(b"x-forwarded-for", b"203.0.113.9")]}
    assert rg.client_key(direct) == "198.51.100.4"  # a header from a stranger is not believed
    assert rg.client_key({}) == "unknown"


# --- the sliding window ---------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_window_allows_the_limit_then_waits_for_the_oldest_event_to_leave():
    clock = Clock()
    limiter = rg.SlidingWindowLimiter(3, 60.0, clock)
    for _ in range(3):
        assert limiter.retry_after("k") == 0.0
        limiter.record("k")
        clock.now += 10
    assert limiter.retry_after("k") == pytest.approx(30.0)  # the first event was 30 s ago
    assert limiter.retry_after("other") == 0.0  # keys are independent
    clock.now += 30.1
    assert limiter.retry_after("k") == 0.0


def test_the_window_forgets_keys_so_a_flood_of_new_ones_cannot_fill_memory():
    clock = Clock()
    limiter = rg.SlidingWindowLimiter(1, 60.0, clock, max_keys=50)
    for i in range(500):
        limiter.record(f"k{i}")
    assert len(limiter._events) <= 50


# --- kill switch and audit file -------------------------------------------------------------------------------------


def test_the_kill_switch_is_a_file_in_the_data_dir_or_in_the_home_locus_folder(tmp_path):
    data = tmp_path / "data"
    assert rg.kill_switch_active(data) is False
    data.mkdir()
    (data / "REMOTE_OFF").write_text("")
    assert rg.kill_switch_active(data) is True
    (data / "REMOTE_OFF").unlink()
    home_switch = tmp_path / "home" / ".locus" / "REMOTE_OFF"
    home_switch.parent.mkdir(parents=True)
    home_switch.write_text("")
    assert rg.kill_switch_active(data) is True


def test_the_audit_line_has_counts_and_paths_and_the_file_is_private(tmp_path):
    log = rg.AuditLog(tmp_path / "data", wall_clock=lambda: 1_700_000_000)
    log.write(http="POST", method="tools/call", tool="locus_search", status=200, results=2, bytes_returned=123,
              paths=["/a/b.md", "/a/c.md"])  # fmt: skip
    (target,) = (tmp_path / "data" / "audit").glob("remote-2023-11-14.jsonl")
    assert json.loads(target.read_text()) == {
        "ts": "2023-11-14T22:13:20+00:00", "http": "POST", "method": "tools/call", "tool": "locus_search",
        "status": 200, "results": 2, "bytes": 123, "paths": ["/a/b.md", "/a/c.md"],
    }  # fmt: skip
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_a_long_path_list_is_cut_and_counted(tmp_path):
    log = rg.AuditLog(tmp_path)
    log.write(http="POST", method="tools/call", tool="locus_list_sources", status=200, results=500,
              bytes_returned=1, paths=[f"/p/{i}.md" for i in range(500)])  # fmt: skip
    (target,) = (tmp_path / "audit").glob("*.jsonl")
    line = json.loads(target.read_text())
    assert len(line["paths"]) == rg.MAX_AUDIT_PATHS and line["paths_total"] == 500


def test_an_audit_log_that_cannot_be_written_never_raises(tmp_path, capsys):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the folder should be")
    log = rg.AuditLog(blocker)
    for _ in range(2):
        log.write(http="GET", method=None, tool=None, status=401, results=None, bytes_returned=0, paths=[])
    assert capsys.readouterr().err.count("audit log not written") == 1


# --- what is read from a request and an answer ----------------------------------------------------------------------


def _call(tool, arguments=None):
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments or {}}}
    ).encode()


def test_a_request_gives_its_method_and_tool_and_nothing_else():
    info = rg.summarize_request(_call("locus_search", {"query": "private words", "top_k": 3}))
    assert info == rg.RequestInfo(method="tools/call", tool="locus_search", path_argument=None)
    assert rg.summarize_request(b'{"method": "tools/list"}') == rg.RequestInfo(method="tools/list")
    assert rg.summarize_request(_call("locus_read_file", {"path": "/a/b.md"})).path_argument == "/a/b.md"
    assert rg.summarize_request(_call("x" * 100)).tool == "(invalid)"
    assert rg.summarize_request(_call("bad name\nwith newline")).tool == "(invalid)"
    for odd in (b"", b"not json", b"[1, 2]", b"42", b'{"method": 7}'):
        assert rg.summarize_request(odd).tool is None


def _answer(text, **extra):
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], **extra}}
    ).encode()


KNOWN = {"/v/a.md", "/v/b.md", "/v/c.md"}


def test_a_search_answer_counts_its_headers_and_keeps_only_indexed_paths():
    text = (
        "# 3 results for: private words\n[Untrusted file content]\n\n"
        "## 1. /v/a.md  (chunk 0, score 0.9, 10 chars)\nsome chunk text\n\n"
        "## 2. /v/b.md  (chunk 2, score 0.8, 10 chars)\n## 3. /v/zzz.md  (chunk 1, score 0.1, 10 chars)\n"
        "## 4. FORGED  (chunk 9, score 0.1, 3 chars)\n"
    )
    count, paths = rg.summarize_response(
        rg.RequestInfo("tools/call", "locus_search"), _answer(text), "application/json", lambda: KNOWN
    )
    assert (count, paths) == (2, ["/v/a.md", "/v/b.md"])


def test_a_list_answer_and_a_read_answer():
    listing = "# Indexed files\ntotal: 3\n- /v/a.md\n- /v/c.md\n- /nope.md\n"
    assert rg.summarize_response(
        rg.RequestInfo("tools/call", "locus_list_sources"), _answer(listing), "application/json", lambda: KNOWN
    ) == (2, ["/v/a.md", "/v/c.md"])
    read = rg.RequestInfo("tools/call", "locus_read_file", "/v/b.md")
    assert rg.summarize_response(
        read, _answer("# /v/b.md (chars 0-5 of 5)\nhello"), "application/json", lambda: KNOWN
    ) == (
        1,
        ["/v/b.md"],
    )
    assert rg.summarize_response(read, _answer("Error: not an indexed file"), "application/json", lambda: KNOWN) == (
        0,
        [],
    )


def test_the_aliases_report_their_ids_and_an_error_answer_has_no_results():
    structured = {"results": [{"id": "/v/a.md#2", "title": "a.md", "url": "file:///v/a.md"}], "content_notice": "x"}
    assert rg.summarize_response(
        rg.RequestInfo("tools/call", "search"), _answer("{}", structuredContent=structured), "application/json", None
    ) == (1, ["/v/a.md"])
    fetched = {"metadata": {"path": "/v/b.md", "chunk_index": 0}}
    assert rg.summarize_response(
        rg.RequestInfo("tools/call", "fetch"), _answer("{}", structuredContent=fetched), "application/json", None
    ) == (1, ["/v/b.md"])
    assert rg.summarize_response(
        rg.RequestInfo("tools/call", "locus_search"), _answer("boom", isError=True), "application/json", lambda: KNOWN
    ) == (0, [])


def test_an_event_stream_answer_is_read_too_and_a_failing_path_lookup_is_harmless():
    event = "event: message\ndata: " + _answer("## 1. /v/a.md  (chunk 0, score 0.9, 1 chars)").decode() + "\n\n"
    info = rg.RequestInfo("tools/call", "locus_search")
    assert rg.summarize_response(info, event.encode(), "text/event-stream", lambda: KNOWN) == (1, ["/v/a.md"])

    def boom() -> set[str]:
        raise RuntimeError("index closed")

    assert rg.summarize_response(
        info, _answer("## 1. /v/a.md  (chunk 0, score 0.9, 1 chars)"), "application/json", boom
    ) == (
        None,
        [],
    )
    assert rg.summarize_response(rg.RequestInfo("tools/list"), b"{}", "application/json", lambda: KNOWN) == (None, [])


# --- the middleware, in order ---------------------------------------------------------------------------------------


async def _drive(app, *, method="POST", headers=(), body=b"", client=("127.0.0.1", 9)):
    scope = {
        "type": "http",
        "method": method,
        "path": "/mcp",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": client,
    }
    inbox = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict] = []

    async def receive():
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent[0]["status"], dict(sent[0]["headers"]), b"".join(m.get("body", b"") for m in sent[1:])


class Echo:
    """A stand-in for the MCP app: answers 200 and keeps what it received, so replaying the body can be checked."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.scopes: list[str] = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope["type"])
        if scope["type"] != "http":
            return
        message = await receive()
        self.bodies.append(message["body"])
        reply = b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ok"}]}}'
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": reply})


def _guard(tmp_path, clock=None, **kw):
    inner = Echo()
    guard = rg.RemoteGuard(
        inner,
        authenticate=rg.static_token_authenticator(TOKEN),
        data_dir=tmp_path / "data",
        clock=clock or Clock(),
        **kw,
    )
    return guard, inner


AUTH = [("Authorization", f"Bearer {TOKEN}")]


def test_the_checks_run_in_the_documented_order(tmp_path):
    clock = Clock()
    guard, inner = _guard(tmp_path, clock, expires_in=100, rate_per_min=2)
    data = tmp_path / "data"
    data.mkdir()

    async def go():
        assert (await _drive(guard, headers=AUTH, body=b"{}"))[0] == 200
        # rate limit: 2 a minute for this token
        assert (await _drive(guard, headers=AUTH, body=b"{}"))[0] == 200
        status, headers, _ = await _drive(guard, headers=AUTH, body=b"{}")
        assert status == 429 and int(headers[b"retry-after"]) >= 1
        clock.now += 61
        assert (await _drive(guard, headers=AUTH, body=b"{}"))[0] == 200
        # the kill switch beats a valid token
        (data / "REMOTE_OFF").write_text("")
        assert (await _drive(guard, headers=AUTH, body=b"{}"))[0] == 503
        (data / "REMOTE_OFF").unlink()
        # the expiry beats a valid token
        clock.now += 100
        status, _, text = await _drive(guard, headers=AUTH, body=b"{}")
        assert status == 401 and b"expired" in text

    asyncio.run(go())


def test_the_request_body_reaches_the_app_unchanged_and_lifespan_passes_through(tmp_path):
    guard, inner = _guard(tmp_path)
    body = _call("locus_list_sources", {"limit": 3})
    assert asyncio.run(_drive(guard, headers=AUTH, body=body))[0] == 200
    assert inner.bodies == [body]

    async def lifespan():
        async def receive():
            return {"type": "lifespan.startup"}

        async def send(_message):
            return None

        await guard({"type": "lifespan"}, receive, send)

    asyncio.run(lifespan())
    assert inner.scopes[-1] == "lifespan"


def test_a_wrong_token_never_reaches_the_app_and_gets_a_www_authenticate_header(tmp_path):
    guard, inner = _guard(tmp_path)
    status, headers, text = asyncio.run(_drive(guard, headers=[("Authorization", "Bearer nope")]))
    assert status == 401 and headers[b"www-authenticate"].startswith(b"Bearer") and inner.bodies == []
    assert b"nope" not in text


def test_failed_attempts_are_counted_per_forwarded_address(tmp_path):
    guard, _ = _guard(tmp_path)

    def attempt(forwarded, token="wrong"):
        return asyncio.run(
            _drive(guard, headers=[("Authorization", f"Bearer {token}"), ("X-Forwarded-For", forwarded)])
        )[0]

    assert [attempt("203.0.113.1") for _ in range(10)] == [401] * 10
    assert attempt("203.0.113.1") == 429
    assert attempt("203.0.113.1", TOKEN) == 429
    assert attempt("203.0.113.2", TOKEN) == 200  # somebody else is not locked out by this address


def test_the_audit_line_of_a_forwarded_call_has_the_tool_and_the_byte_count_but_not_the_arguments(tmp_path):
    guard, _ = _guard(tmp_path)
    asyncio.run(_drive(guard, headers=AUTH, body=_call("locus_search", {"query": "secret words"})))
    (target,) = (tmp_path / "data" / "audit").glob("*.jsonl")
    raw = target.read_text()
    line = json.loads(raw)
    assert (line["http"], line["method"], line["tool"], line["status"]) == ("POST", "tools/call", "locus_search", 200)
    assert line["bytes"] > 0 and "secret words" not in raw and TOKEN not in raw


def test_with_the_audit_off_nothing_is_written(tmp_path):
    guard, _ = _guard(tmp_path, audit=False)
    assert asyncio.run(_drive(guard, headers=AUTH, body=b"{}"))[0] == 200
    assert not (tmp_path / "data" / "audit").exists()


def test_the_remote_tools_are_the_read_only_ones_and_the_two_local_tools_are_apart():
    assert set(rg.REMOTE_TOOLS).isdisjoint(rg.LOCAL_ONLY_TOOLS)
    assert {"locus_search", "locus_list_sources", "locus_read_file", "locus_help", "search", "fetch"} == set(
        rg.REMOTE_TOOLS
    )
    assert Path(rg.__file__).read_text().count("from .api") == 0 and "import api" not in Path(rg.__file__).read_text()
