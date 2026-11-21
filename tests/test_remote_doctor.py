"""`locus doctor --remote <url>`: against the real server on a local port, and against small fake servers that fail in
each way the checks know. No internet: every address here is 127.0.0.1."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("chromadb")

from locus import cli, remote_guard, service  # noqa: E402
from locus import mcp_server as srv  # noqa: E402
from locus.remote_doctor import RemoteDoctor, run_remote_doctor  # noqa: E402

from .remote_support import TOKEN, preserved_server_state, serve_app  # noqa: E402

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
GOOD_TOOLS = [{"name": n, "annotations": READ_ONLY} for n in remote_guard.REMOTE_TOOLS]


def _run(url: str, token: str | None = TOKEN) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = run_remote_doctor(url, token, timeout=10, out=lines.append)
    return code, lines


def _status(lines: list[str], label: str) -> str:
    (line,) = [x for x in lines if f"] {label}" in x]
    return line[1:5].strip()


class FakeServer:
    """A server that answers like Locus does, or fails in one chosen way."""

    def __init__(self, *, get_status=401, init="json", tools=None, session=None, init_status=200):
        self.get_status, self.init, self.session, self.init_status = get_status, init, session, init_status
        self.tools = GOOD_TOOLS if tools is None else tools
        self.seen: list[tuple[str, str]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _send(self, status, body=b"", ctype="application/json", extra=()):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                fake.seen.append(("GET", self.headers.get("Authorization", "")))
                self._send(fake.get_status, b"nope", "text/plain")

            def do_POST(self):  # noqa: N802
                fake.seen.append(("POST", self.headers.get("Authorization", "")))
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, b"no", "text/plain")
                method = body.get("method")
                if method == "initialize":
                    if fake.init_status != 200:
                        return self._send(fake.init_status, b"x", "text/plain")
                    result = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "fake"}}}
                    extra = [("Mcp-Session-Id", fake.session)] if fake.session else []
                    if fake.init == "sse":
                        return self._send(200, f"data: {json.dumps(result)}\n\n".encode(), "text/event-stream", extra)
                    if fake.init == "html":
                        return self._send(200, b"<html></html>", "text/html", extra)
                    return self._send(200, json.dumps(result).encode(), extra=extra)
                if method == "tools/list":
                    if fake.session and self.headers.get("Mcp-Session-Id") != fake.session:
                        return self._send(400, b"no session", "text/plain")
                    return self._send(
                        200, json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": fake.tools}}).encode()
                    )
                self._send(202)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# --- against the real server ----------------------------------------------------------------------------------------


@pytest.fixture
def real(demo_index, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(srv, "_components", lambda: (demo_index.embedder, demo_index.store, demo_index.settings))
    monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
    options = remote_guard.RemoteOptions(public_hosts=("example.test",), data_dir=tmp_path / "data")
    with preserved_server_state():
        with serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)) as port:
            yield f"http://127.0.0.1:{port}/mcp", options


def test_every_check_passes_against_a_correctly_started_server(real):
    url, _ = real
    code, lines = _run(url)
    assert code == 0, lines
    labels = ("DNS", "TLS", "no token -> 401", "initialize with the token", "tools/list")
    assert [_status(lines, label) for label in labels] == ["OK", "WARN", "OK", "OK", "OK"]
    assert not any("FAIL" in line for line in lines)
    assert "All remote checks passed." in lines[-1]


def test_the_token_is_never_printed(real):
    url, _ = real
    for token in (TOKEN, "a-wrong-token-that-must-not-be-echoed-0000"):
        _, lines = _run(url, token)
        assert token not in "\n".join(lines)


def test_a_wrong_token_fails_initialize_and_says_why(real):
    url, _ = real
    code, lines = _run(url, "a-wrong-token-that-must-not-be-echoed-0000")
    assert code == 1
    assert _status(lines, "no token -> 401") == "OK" and _status(lines, "initialize with the token") == "FAIL"
    assert "401" in [x for x in lines if "initialize" in x][0] and _status(lines, "tools/list") == "FAIL"


def test_no_token_at_all_fails_the_two_checks_that_need_it(real):
    url, _ = real
    code, lines = _run(url, None)
    assert code == 1 and "--token-env" in "\n".join(lines)
    assert [_status(lines, "initialize with the token"), _status(lines, "tools/list")] == ["FAIL", "FAIL"]


def test_the_kill_switch_shows_up_as_503_with_the_fix(real):
    url, options = real
    options.data_dir.mkdir(parents=True, exist_ok=True)
    (options.data_dir / "REMOTE_OFF").write_text("")
    code, lines = _run(url)
    assert code == 1 and "REMOTE_OFF" in "\n".join(lines)


def test_a_server_started_with_sse_fails_the_json_check(demo_index, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(srv, "_components", lambda: (demo_index.embedder, demo_index.store, demo_index.settings))
    options = remote_guard.RemoteOptions(public_hosts=("example.test",), data_dir=tmp_path / "data", sse=True)
    with preserved_server_state(), serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)) as port:
        code, lines = _run(f"http://127.0.0.1:{port}/mcp")
    assert code == 1 and _status(lines, "initialize with the token") == "FAIL"
    assert "Server-Sent Events" in "\n".join(lines)


# --- against fake servers -------------------------------------------------------------------------------------------


def test_a_fake_server_that_behaves_passes():
    with FakeServer() as fake:
        code, lines = _run(fake.url)
    assert code == 0, lines
    assert fake.seen[0] == ("GET", "")  # the unauthenticated probe carries no token


def test_a_server_that_answers_200_without_a_token_is_a_security_failure():
    with FakeServer(get_status=200) as fake:
        code, lines = _run(fake.url)
    assert code == 1 and "SECURITY" in "\n".join(lines)


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (421, "LOCUS_MCP_PUBLIC_HOST"),
        (403, "firewall"),
        (404, "path"),
        (530, "does not answer"),
        (429, "Wait a minute"),
    ],
)
def test_a_wrong_answer_to_the_unauthenticated_request_names_the_fix(status, needle):
    with FakeServer(get_status=status) as fake:
        code, lines = _run(fake.url)
    assert code == 1 and needle in [x for x in lines if "no token" in x][0]


def test_an_initialize_that_comes_back_as_an_event_stream_or_not_json_fails():
    with FakeServer(init="sse") as fake:
        code, lines = _run(fake.url)
    assert code == 1 and "Server-Sent Events" in "\n".join(lines)
    with FakeServer(init="html") as fake:
        code, lines = _run(fake.url)
    assert code == 1 and "not a JSON reply" in "\n".join(lines)
    with FakeServer(init_status=421) as fake:
        code, lines = _run(fake.url)
    assert code == 1 and "LOCUS_MCP_PUBLIC_HOST" in "\n".join(lines)


def test_a_server_that_keeps_sessions_is_still_checked_through_to_tools_list():
    with FakeServer(session="abc123") as fake:
        code, lines = _run(fake.url)
    assert code == 0, lines


@pytest.mark.parametrize(
    ("tools", "needle"),
    [
        (
            GOOD_TOOLS + [{"name": "locus_diagnose_connection", "annotations": READ_ONLY}],
            "locus_diagnose_connection should not",
        ),
        (GOOD_TOOLS[1:], "missing locus_search"),
        (GOOD_TOOLS[:-1] + [{"name": "fetch", "annotations": {"readOnlyHint": False}}], "not marked read-only: fetch"),
        (GOOD_TOOLS[:-1] + [{"name": "fetch"}], "not marked read-only: fetch"),
    ],
)
def test_the_tool_check_wants_the_expected_names_and_the_read_only_hint(tools, needle):
    with FakeServer(tools=tools) as fake:
        code, lines = _run(fake.url)
    assert code == 1 and needle in "\n".join(lines)


def test_nothing_listening_gives_a_failure_not_a_traceback():
    with FakeServer() as fake:
        url = fake.url
    code, lines = _run(url)
    assert code == 1 and _status(lines, "no token -> 401") == "FAIL" and "no answer" in "\n".join(lines)


def test_an_unresolvable_name_fails_the_dns_line_and_the_rest_fails_too():
    code, lines = _run("http://no-such-host.invalid/mcp")
    assert code == 1 and _status(lines, "DNS") == "FAIL" and "does not resolve" in lines[0]


@pytest.mark.parametrize(
    "url", ["not a url", "ftp://abc.test/mcp", "https://user:pw@abc.test/mcp", "https://abc.test/mcp?token=secret-123"]
)
def test_addresses_that_are_not_plain_web_addresses_are_refused_up_front(url):
    code, lines = _run(url)
    assert code == 1 and lines[0].startswith("[FAIL] address")
    assert "secret-123" not in "\n".join(lines) and "pw@" not in "\n".join(lines)


def test_an_https_address_on_another_port_fails_the_443_line():
    doctor_lines: list[str] = []
    doctor = RemoteDoctor("https://127.0.0.1:1/mcp", TOKEN, timeout=3, out=doctor_lines.append)
    doctor.run()
    assert _status(doctor_lines, "port 443") == "FAIL" and _status(doctor_lines, "TLS") == "FAIL"


# --- the command ----------------------------------------------------------------------------------------------------


def test_the_command_reads_the_token_from_the_named_variable_and_exits_0(real, monkeypatch, capsys):
    url, _ = real
    monkeypatch.delenv("LOCUS_MCP_TOKEN", raising=False)
    monkeypatch.setenv("MY_LOCUS_TOKEN", TOKEN)
    with pytest.raises(SystemExit) as stop:
        cli.main(["doctor", "--remote", url, "--token-env", "MY_LOCUS_TOKEN"])
    out = capsys.readouterr().out
    assert stop.value.code == 0 and "All remote checks passed." in out and TOKEN not in out


def test_the_command_defaults_to_locus_mcp_token_and_fails_with_exit_1_on_a_wrong_one(real, monkeypatch, capsys):
    url, _ = real
    monkeypatch.setenv("LOCUS_MCP_TOKEN", "wrong-token-from-the-environment-000000")
    with pytest.raises(SystemExit) as stop:
        cli.main(["doctor", "--remote", url])
    out = capsys.readouterr().out
    assert stop.value.code == 1 and "wrong-token-from-the-environment" not in out


def test_token_env_without_remote_is_an_error(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["doctor", "--token-env", "X"])
    assert stop.value.code == 2 and "--remote" in capsys.readouterr().err


def test_plain_doctor_has_the_new_arguments_unset():
    args = cli.build_parser().parse_args(["doctor"])
    assert (args.remote, args.token_env) == (None, None)


def test_serve_gets_the_new_flags_and_refuses_them_on_other_transports(monkeypatch, capsys):
    seen: list[dict] = []
    monkeypatch.setattr(srv, "main", lambda transport, **kw: seen.append({"transport": transport, **kw}))
    assert cli.cmd_serve(cli.build_parser().parse_args(["serve"])) == 0
    assert seen[-1] == {"transport": "stdio", "host": "127.0.0.1", "port": 8000, "path": "/mcp"}  # unchanged call
    args = cli.build_parser().parse_args(
        [
            "serve",
            "--transport",
            "streamable-http",
            "--public-host",
            "a.test",
            "--expires",
            "2h",
            "--sse",
            "--all-tools",
        ]
    )
    assert cli.cmd_serve(args) == 0
    assert seen[-1] == {
        "transport": "streamable-http", "host": "127.0.0.1", "port": 8000, "path": "/mcp",
        "public_host": "a.test", "sse": True, "all_tools": True, "expires": "2h",
    }  # fmt: skip
    assert cli.cmd_serve(cli.build_parser().parse_args(["serve", "--public-host", "a.test"])) == 2
    assert "only apply to --transport streamable-http" in capsys.readouterr().err
