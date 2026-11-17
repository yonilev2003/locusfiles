"""The streamable-http server in public-host mode (a user-run tunnel), end to end: the real app on a local port, the
demo vault indexed with the offline HashEmbedder, plain HTTP calls with chosen Host headers. Nothing leaves this
computer."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as srv  # noqa: E402
from locus import remote_guard, service  # noqa: E402
from locus.config import get_settings  # noqa: E402

from .demo_index import QUERIES  # noqa: E402
from .remote_support import (  # noqa: E402
    INITIALIZE,
    TOKEN,
    TOOLS_LIST,
    call_tool,
    http_call,
    preserved_server_state,
    rpc,
    serve_app,
)

PUBLIC = "example.test"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No REMOTE_OFF file of the real user, no announce setting, the demo vault as the one open index."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOCUS_ANNOUNCE", raising=False)
    monkeypatch.delenv("LOCUS_API_KEY", raising=False)
    with preserved_server_state():
        yield


@pytest.fixture
def wired(demo_index, monkeypatch):
    monkeypatch.setattr(srv, "_components", lambda: (demo_index.embedder, demo_index.store, demo_index.settings))
    monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
    monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
    return demo_index


def _options(tmp_path: Path, **changes) -> remote_guard.RemoteOptions:
    base = remote_guard.RemoteOptions(public_hosts=(PUBLIC,), data_dir=tmp_path / "locus-data")
    return replace(base, **changes)


@pytest.fixture
def server(wired, tmp_path):
    """(port, options) of a public-host server with the default options."""
    options = _options(tmp_path)
    with serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)) as port:
        yield SimpleNamespace(port=port, options=options, data_dir=options.data_dir, index=wired)


def _tool_names(port: int) -> dict[str, dict]:
    reply = rpc(port, TOOLS_LIST)
    assert reply.status == 200, reply.text
    return {t["name"]: t for t in reply.json()["result"]["tools"]}


def _audit_lines(data_dir: Path) -> list[dict]:
    lines = []
    for f in sorted((data_dir / "audit").glob("remote-*.jsonl")):
        lines += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
    return lines


# --- G1: the public host passes the Host/Origin guard, nothing else does --------------------------------------------


def test_the_public_host_is_accepted_and_any_other_host_gets_421(server):
    assert rpc(server.port, INITIALIZE, host=PUBLIC).status == 200
    assert rpc(server.port, INITIALIZE, host=f"{PUBLIC}:443").status == 200
    assert rpc(server.port, INITIALIZE, host="evil.test").status == 421
    assert rpc(server.port, INITIALIZE, host=f"{PUBLIC}.evil.test").status == 421
    assert rpc(server.port, INITIALIZE, host=f"x{PUBLIC}").status == 421


def test_the_localhost_forms_still_work(server):
    for host in (f"127.0.0.1:{server.port}", f"localhost:{server.port}", f"[::1]:{server.port}"):
        assert rpc(server.port, INITIALIZE, host=host).status == 200, host


def test_a_request_with_no_origin_header_works_and_a_foreign_origin_is_refused(server):
    assert rpc(server.port, INITIALIZE).status == 200  # server-to-server clients send no Origin
    assert rpc(server.port, INITIALIZE, extra={"Origin": f"https://{PUBLIC}"}).status == 200
    assert rpc(server.port, INITIALIZE, extra={"Origin": "https://evil.test"}).status == 403
    assert rpc(server.port, INITIALIZE, extra={"Origin": f"http://{PUBLIC}"}).status == 403  # the tunnel is https


def test_dns_rebinding_protection_stays_on(server):
    # The localhost-only guard of loopback mode and the public-host guard are the same mechanism: a host that is on
    # neither list is refused even with the right token.
    assert rpc(server.port, INITIALIZE, host="10.0.0.5:8000").status == 421


# --- G2: JSON replies, no sessions ----------------------------------------------------------------------------------


def test_a_post_returns_application_json_and_no_session(server):
    reply = rpc(server.port, INITIALIZE)
    assert reply.status == 200
    assert reply.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in reply.headers
    assert reply.json()["result"]["serverInfo"]["name"] == "locus_mcp"


def test_tools_can_be_listed_and_called_without_an_initialize_first(server):
    assert "locus_search" in _tool_names(server.port)
    reply = call_tool(server.port, "locus_list_sources", {"limit": 3})
    assert reply.status == 200 and reply.headers["content-type"].startswith("application/json")


def test_the_sse_flag_gives_back_event_streams(wired, tmp_path):
    options = _options(tmp_path, sse=True)
    with serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)) as port:
        reply = rpc(port, INITIALIZE)
        assert reply.status == 200
        assert reply.headers["content-type"].startswith("text/event-stream")
        assert reply.headers.get("mcp-session-id")


def test_no_locus_tool_sends_progress_or_log_notifications():
    """JSON replies carry the answer only: a progress or log notification would be dropped. None is sent today."""
    source = Path(srv.__file__).read_text(encoding="utf-8") + Path(srv.__file__).with_name("remote_tools.py").read_text(
        encoding="utf-8"
    )
    for call in ("report_progress", "send_progress_notification", "send_log_message", "ctx.info(", "ctx.debug("):
        assert call not in source, call


# --- G5: the remote tool allowlist ----------------------------------------------------------------------------------


def test_over_a_tunnel_only_the_read_only_tools_are_listed(server):
    tools = _tool_names(server.port)
    assert set(tools) == set(remote_guard.REMOTE_TOOLS)
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools.values())
    assert "locus_diagnose_connection" not in tools and "locus_account_status" not in tools


def test_the_two_local_tools_cannot_be_called_over_a_tunnel(server):
    for name in ("locus_diagnose_connection", "locus_account_status"):
        reply = call_tool(server.port, name, {})
        body = reply.json()
        assert reply.status == 200 and ("error" in body or body["result"].get("isError") is True), name
        assert "Unknown tool" in reply.text


def test_the_instructions_do_not_send_the_model_to_a_tool_that_is_not_there(server):
    text = rpc(server.port, INITIALIZE).json()["result"]["instructions"]
    named = set(re.findall(r"\b(?:locus_[a-z_]+|search|fetch)\b", text))
    assert "locus_diagnose_connection" not in text and "locus_account_status" not in text
    assert {"locus_search", "locus_read_file", "locus_help"} <= named


def test_the_account_tool_is_removed_too_when_a_key_is_set(wired, tmp_path, monkeypatch):
    srv.apply_tool_set(SimpleNamespace(managed_api_key="locus_live_TESTkey0123456789abcdefghijklmnop"))
    assert "locus_account_status" in {t.name for t in asyncio.run(srv.mcp.list_tools())}
    with serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=_options(tmp_path))) as port:
        assert set(_tool_names(port)) == set(remote_guard.REMOTE_TOOLS)


def test_all_tools_puts_the_two_local_tools_back(wired, tmp_path):
    options = _options(tmp_path, all_tools=True)
    with serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)) as port:
        names = set(_tool_names(port))
        assert {"locus_diagnose_connection", "locus_search"} <= names


def test_stdio_and_loopback_keep_the_five_tools_and_no_aliases():
    names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    assert names == {"locus_search", "locus_list_sources", "locus_read_file", "locus_help", "locus_diagnose_connection"}


# --- existing tools answer as before --------------------------------------------------------------------------------


@pytest.mark.parametrize("query", QUERIES[:2])
def test_locus_search_over_the_tunnel_returns_the_same_text_as_a_direct_call(server, query):
    direct = asyncio.run(srv.locus_search(query=query, top_k=4))
    reply = call_tool(server.port, "locus_search", {"query": query, "top_k": 4})
    assert reply.json()["result"]["content"][0]["text"] == direct


def test_locus_read_file_over_the_tunnel_keeps_its_confinement(server, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("OUTSIDE-THE-INDEX")
    reply = call_tool(server.port, "locus_read_file", {"path": str(outside)})
    text = reply.json()["result"]["content"][0]["text"]
    assert "is not an indexed file" in text and "OUTSIDE-THE-INDEX" not in text


# --- G4: search and fetch -------------------------------------------------------------------------------------------


def _result(reply) -> dict:
    assert reply.status == 200, reply.text
    return reply.json()["result"]


def test_search_and_fetch_are_listed_read_only_with_an_output_schema(server):
    tools = _tool_names(server.port)
    for name in ("search", "fetch"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
        assert tools[name]["outputSchema"]["type"] == "object"
    assert set(tools["search"]["inputSchema"]["properties"]) == {"query"}
    assert set(tools["fetch"]["inputSchema"]["properties"]) == {"id"}
    assert set(tools["search"]["outputSchema"]["properties"]) >= {"results"}
    assert set(tools["fetch"]["outputSchema"]["properties"]) >= {"id", "title", "text", "url", "metadata"}


def test_search_returns_ids_titles_and_urls_as_structured_content_and_as_json_text(server):
    result = _result(call_tool(server.port, "search", {"query": QUERIES[0]}))
    structured = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == structured
    assert structured["results"], "the demo vault answers this question"
    for item in structured["results"]:
        assert set(item) == {"id", "title", "url"}
        path, _, chunk = item["id"].rpartition("#")
        assert path.startswith(str(server.index.root)) and chunk.isdigit()  # constraint 2: path and chunk_index
        assert item["title"] == Path(path).name and item["url"] == Path(path).as_uri()
    assert "Untrusted file content" in structured["content_notice"]
    direct = service.Engine(components=lambda: (server.index.embedder, server.index.store, server.index.settings))
    expected = [(h.path, h.chunk_index) for h in direct.search(QUERIES[0], k=10).results]
    got = [(i["id"].rpartition("#")[0], int(i["id"].rpartition("#")[2])) for i in structured["results"]]
    assert got == expected


def test_fetch_returns_the_file_text_with_the_untrusted_label_and_the_path_in_metadata(server):
    hit = _result(call_tool(server.port, "search", {"query": QUERIES[1]}))["structuredContent"]["results"][0]
    result = _result(call_tool(server.port, "fetch", {"id": hit["id"]}))
    structured = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == structured
    path, _, chunk = hit["id"].rpartition("#")
    assert set(structured) == {"id", "title", "text", "url", "metadata"}
    assert structured["id"] == hit["id"] and structured["metadata"] == {"path": path, "chunk_index": int(chunk)}
    assert structured["text"].startswith(srv.UNTRUSTED_NOTICE)
    assert Path(path).read_text(encoding="utf-8").strip()[:40] in structured["text"]
    assert structured["url"] == Path(path).as_uri()


def test_fetch_refuses_anything_that_is_not_an_indexed_file(server, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("OUTSIDE-THE-INDEX")
    for bad in (f"{outside}#0", "/etc/hosts#0", "no-hash-here", "#3", f"{server.index.root}/missing.md#0"):
        result = _result(call_tool(server.port, "fetch", {"id": bad}))
        assert result["isError"] is True, bad
        assert "OUTSIDE-THE-INDEX" not in json.dumps(result), bad


@pytest.mark.skipif(os.name == "nt", reason="needs a symlink")
def test_fetch_refuses_an_indexed_path_that_became_a_link_out_of_the_index(server, tmp_path):
    """The same confinement as locus_read_file: an indexed path swapped for a link to somewhere else is refused."""
    root = tmp_path / "corpus"
    root.mkdir()
    victim = root / "note.md"
    victim.write_text("indexed content about the zebra release " * 5)
    from locus.ingest.indexer import index_folder

    index_folder(root, server.index.embedder, server.index.store, server.index.settings, log=lambda _m: None)
    secret = tmp_path / "outside.md"
    secret.write_text("OUTSIDE-THE-INDEX-SECRET-4711")
    assert (
        "zebra"
        in _result(call_tool(server.port, "fetch", {"id": f"{victim.resolve()}#0"}))["structuredContent"]["text"]
    )
    victim.unlink()
    victim.symlink_to(secret)
    result = _result(call_tool(server.port, "fetch", {"id": f"{victim.resolve()}#0"}))
    assert "OUTSIDE-THE-INDEX-SECRET-4711" not in json.dumps(result)
    server.index.store.delete_path(str(root.resolve() / "note.md"))


def test_the_chatgpt_tools_can_be_switched_off(wired, tmp_path):
    options = _options(tmp_path, chatgpt_tools=False)
    with serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)) as port:
        assert set(_tool_names(port)) == {"locus_search", "locus_list_sources", "locus_read_file", "locus_help"}


# --- G6: the safety floor -------------------------------------------------------------------------------------------


def test_a_request_without_the_token_or_with_a_wrong_one_gets_401(server):
    assert rpc(server.port, INITIALIZE, token=None).status == 401
    assert rpc(server.port, INITIALIZE, token="wrong").status == 401
    assert rpc(server.port, INITIALIZE, token=TOKEN[:-1]).status == 401
    assert rpc(server.port, INITIALIZE, token=TOKEN + "x").status == 401
    assert http_call(server.port, "GET", "/mcp", headers={"Host": PUBLIC}).status == 401
    assert http_call(server.port, "GET", "/anything-else", headers={"Host": PUBLIC}).status == 401


def test_a_non_ascii_authorization_header_is_a_401_not_an_error(server):
    reply = http_call(
        server.port, "POST", "/mcp", headers={"Host": PUBLIC, "Authorization": "Bearer h\xe9llo"}, body=b"{}"
    )
    assert reply.status == 401


def test_the_rate_limit_answers_429_with_retry_after(wired, tmp_path):
    options = _options(tmp_path, rate_per_min=3)
    with serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)) as port:
        assert [rpc(port, INITIALIZE).status for _ in range(3)] == [200, 200, 200]
        reply = rpc(port, INITIALIZE)
        assert reply.status == 429 and int(reply.headers["retry-after"]) >= 1


def test_ten_failed_attempts_a_minute_then_429_even_for_the_right_token(server):
    assert [rpc(server.port, INITIALIZE, token="wrong").status for _ in range(10)] == [401] * 10
    assert rpc(server.port, INITIALIZE, token="wrong").status == 429
    reply = rpc(server.port, INITIALIZE)
    assert reply.status == 429 and "retry-after" in reply.headers


def test_the_kill_switch_file_gives_503_on_every_request_while_it_exists(server):
    assert rpc(server.port, INITIALIZE).status == 200
    server.data_dir.mkdir(parents=True, exist_ok=True)
    switch = server.data_dir / remote_guard.KILL_SWITCH_FILE
    switch.write_text("")
    try:
        assert rpc(server.port, INITIALIZE).status == 503
        assert rpc(server.port, INITIALIZE, token="wrong").status == 503
        assert http_call(server.port, "GET", "/mcp").status == 503
    finally:
        switch.unlink()
    assert rpc(server.port, INITIALIZE).status == 200


def test_the_kill_switch_in_the_home_locus_folder_works_when_the_data_dir_is_elsewhere(server, tmp_path):
    switch = tmp_path / "home" / ".locus" / remote_guard.KILL_SWITCH_FILE
    switch.parent.mkdir(parents=True)
    switch.write_text("")
    assert rpc(server.port, INITIALIZE).status == 503


def test_after_the_expiry_every_request_gets_401(wired, tmp_path):
    options = _options(tmp_path, expires_s=0.6)
    with serve_app(srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)) as port:
        assert rpc(port, INITIALIZE).status == 200
        time.sleep(0.8)
        reply = rpc(port, INITIALIZE)
        assert reply.status == 401 and "expired" in reply.text
        assert http_call(port, "GET", "/mcp").status == 401


def test_the_audit_log_holds_counts_and_paths_and_never_the_query_a_chunk_or_the_token(server):
    secret_query = "zebra-unicorn-needle-4711 pricing tiers"
    reply = call_tool(server.port, "locus_search", {"query": secret_query, "top_k": 3})
    text = reply.json()["result"]["content"][0]["text"]
    assert secret_query in text  # the answer does repeat the query: the log must not
    chunk_text = text.split("\n\n")[3][:60]
    call_tool(server.port, "locus_list_sources", {"limit": 5})
    call_tool(server.port, "search", {"query": secret_query})
    rpc(server.port, INITIALIZE, token="wrong-token-value-9999")
    lines = _audit_lines(server.data_dir)
    by_tool = {line["tool"]: line for line in lines if line["tool"]}
    search = by_tool["locus_search"]
    assert search["status"] == 200 and search["method"] == "tools/call" and search["http"] == "POST"
    assert search["results"] == 3 and len(search["paths"]) >= 1 and search["bytes"] > 1000
    indexed = set(server.index.store.list_paths())
    assert set(search["paths"]) <= indexed
    assert by_tool["locus_list_sources"]["results"] == 5
    assert by_tool["search"]["results"] >= 1
    assert any(line["status"] == 401 and line["method"] is None for line in lines)
    raw = "".join(f.read_text(encoding="utf-8") for f in (server.data_dir / "audit").glob("*.jsonl"))
    for forbidden in ("zebra-unicorn-needle-4711", chunk_text, TOKEN, "wrong-token-value-9999", "Bearer"):
        assert forbidden not in raw, forbidden
    if os.name != "nt":
        audit_dir = server.data_dir / "audit"
        assert stat.S_IMODE(audit_dir.stat().st_mode) == 0o700
        for f in audit_dir.glob("*.jsonl"):
            assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_the_audit_log_logs_a_path_only_when_it_is_indexed(server):
    """A chunk that imitates a result header must not put its own text in the log."""
    forged = remote_guard.summarize_response(
        remote_guard.RequestInfo(method="tools/call", tool="locus_search"),
        json.dumps(
            {
                "result": {
                    "content": [{"type": "text", "text": "## 1. FORGED SECRET TEXT  (chunk 0, score 1.0, 3 chars)"}]
                }
            }
        ).encode(),
        "application/json",
        lambda: {"/real/indexed.md"},
    )
    assert forged == (0, [])


# --- starting it: main() --------------------------------------------------------------------------------------------


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("LOCUS_MCP_PUBLIC_HOST", raising=False)
    monkeypatch.delenv("LOCUS_MCP_RATE_PER_MIN", raising=False)
    monkeypatch.delenv("LOCUS_CHATGPT_TOOLS", raising=False)
    get_settings.cache_clear()
    started: list[dict] = []
    monkeypatch.setattr(srv, "start_warmup", lambda: None)
    monkeypatch.setattr(srv, "_run_http", lambda **kwargs: started.append(kwargs))
    yield started
    get_settings.cache_clear()


def test_without_a_public_host_main_starts_the_loopback_server_exactly_as_before(main_env):
    srv.main("streamable-http")
    assert main_env == [{"host": "127.0.0.1", "port": 8000, "path": "/mcp", "token": TOKEN}]


def test_a_short_token_is_fine_on_loopback_but_refused_with_a_public_host(main_env, monkeypatch, capsys):
    monkeypatch.setenv("LOCUS_MCP_TOKEN", "short-secret")
    get_settings.cache_clear()
    srv.main("streamable-http")
    assert len(main_env) == 1
    with pytest.raises(SystemExit) as stop:
        srv.main("streamable-http", public_host=PUBLIC)
    err = capsys.readouterr().err
    assert stop.value.code == 2 and "at least 32" in err and "short-secret" not in err and len(main_env) == 1


def test_the_public_host_comes_from_the_flag_or_the_environment(main_env, monkeypatch):
    srv.main("streamable-http", public_host="a.example.test, https://b.example.test/mcp")
    assert main_env[0]["remote"].public_hosts == ("a.example.test", "b.example.test")
    monkeypatch.setenv("LOCUS_MCP_PUBLIC_HOST", "env.example.test")
    srv.main("streamable-http")
    remote = main_env[1]["remote"]
    assert remote.public_hosts == ("env.example.test",) and remote.expires_s == 8 * 3600 and remote.sse is False
    assert main_env[1]["host"] == "127.0.0.1"


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"public_host": PUBLIC, "host": "0.0.0.0"}, "127.0.0.1"),
        ({"public_host": "*.example.test"}, "not a plain host name"),
        ({"public_host": PUBLIC, "expires": "soon"}, "--expires"),
        ({"public_host": PUBLIC, "expires": "8"}, "unit"),
        ({"sse": True}, "only apply with a public host"),
        ({"all_tools": True}, "only apply with a public host"),
        ({"expires": "1h"}, "only apply with a public host"),
    ],
)
def test_bad_public_host_settings_stop_the_start_with_exit_2(main_env, capsys, kwargs, needle):
    with pytest.raises(SystemExit) as stop:
        srv.main("streamable-http", **kwargs)
    assert stop.value.code == 2 and needle in capsys.readouterr().err and main_env == []


def test_expires_zero_turns_the_expiry_off_and_the_rate_comes_from_the_environment(main_env, monkeypatch):
    monkeypatch.setenv("LOCUS_MCP_RATE_PER_MIN", "30")
    monkeypatch.setenv("LOCUS_CHATGPT_TOOLS", "0")
    srv.main("streamable-http", public_host=PUBLIC, expires="0")
    remote = main_env[0]["remote"]
    assert (remote.expires_s, remote.rate_per_min, remote.chatgpt_tools) == (None, 30, False)


def test_the_startup_banner_never_prints_the_token(wired, tmp_path, capsys):
    options = _options(tmp_path)
    srv.build_remote_app(path="/mcp", token=TOKEN, remote=options)
    srv._print_remote_banner(host="127.0.0.1", port=8000, path="/mcp", remote=options)
    err = capsys.readouterr().err
    assert TOKEN not in err and f"https://{PUBLIC}/mcp" in err and "kill switch" in err
