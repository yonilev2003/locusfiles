"""`locus query --json`, `locus sources` and `locus read`: JSON on stdout only, the same paths and chunk indexes as the
MCP tools, and the documented exit codes. The text output of `locus query` is unchanged. Offline: HashEmbedder."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

pytest.importorskip("chromadb")

from locus import (
    cli,  # noqa: E402
    service,  # noqa: E402
)
from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.errors import AuthFailed  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.policy import Policy, matcher_for_folder  # noqa: E402

from .demo_index import QUERIES, mcp_hits  # noqa: E402


def _wire(monkeypatch, embedder, store, settings) -> None:
    """The CLI, the MCP tools and the service all open this one index; no freshness step."""
    components = (embedder, store, settings)
    monkeypatch.setattr(service, "_open_default", lambda: components)
    monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_components", lambda: components)
    monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_components", lambda _settings: (embedder, store))


@pytest.fixture
def world(demo_index, monkeypatch):
    _wire(monkeypatch, demo_index.embedder, demo_index.store, demo_index.settings)
    return demo_index


def run(capsys, *argv):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(list(argv))
    captured = capsys.readouterr()
    return stop.value.code, captured.out, captured.err


# --- query --json ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("query", QUERIES)
def test_query_json_gives_the_hits_the_mcp_tool_gives(world, capsys, query):
    code, out, err = run(capsys, "query", query, "-k", "6", "--json")
    payload = json.loads(out)  # stdout is one JSON document and nothing else
    assert code == 0 and err == ""
    assert payload["schema"] == "locus.query.v1" and payload["content_notice"] == service.CONTENT_NOTICE
    mcp = mcp_hits(asyncio.run(srv.locus_search(query=query, top_k=6)))
    got = [(r["path"], r["chunk_index"]) for r in payload["results"]]
    assert got == [(h["path"], h["chunk_index"]) for h in mcp] and len(got) == 6
    assert all(r["text"] and r["modified"] for r in payload["results"])


def test_query_json_honours_k_and_prefix(world, capsys):
    folder = str(world.root / "pricing")
    code, out, _ = run(capsys, "query", "price", "-k", "2", "--prefix", folder, "--json")
    results = json.loads(out)["results"]
    assert code == 0 and len(results) == 2 and all(r["path"].startswith(folder + "/") for r in results)


def test_query_json_with_a_prefix_that_holds_nothing_is_an_empty_list_and_exit_0(world, capsys):
    code, out, _ = run(capsys, "query", "price", "--prefix", "/no/such/folder", "--json")
    payload = json.loads(out)
    assert code == 0 and payload["results"] == [] and payload["note"]


def test_non_ascii_text_is_written_as_text_not_as_escapes(tmp_path, monkeypatch, capsys):
    settings = Settings(
        data_dir=tmp_path / "data", collection="hebrew", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, "hebrew", embedder.name)
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("שלום עולם, זו הערה על התמחור של המוצר")
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    _wire(monkeypatch, embedder, store, settings)
    code, out, _ = run(capsys, "query", "התמחור", "--json")
    assert code == 0 and "שלום עולם" in out and "\\u" not in out
    assert json.loads(out)["results"][0]["text"].startswith("שלום")


def test_the_text_output_of_query_is_unchanged(world, capsys):
    hits = world.store.query(world.embedder.embed_query(QUERIES[0]), k=3)
    expected = ""
    for i, hit in enumerate(hits, 1):
        snippet = hit.text.replace("\n", " ")
        snippet = snippet if len(snippet) <= 60 else snippet[:60] + "…"
        expected += f"{i}. [{hit.score:.3f}] {hit.path} #{hit.chunk_index}\n   {snippet}\n"
    code, out, err = run(capsys, "query", QUERIES[0], "-k", "3", "--width", "60")
    assert (code, out, err) == (0, expected, "")


def test_the_text_query_with_no_hit_still_exits_1(world, capsys):
    code, out, _ = run(capsys, "query", "price", "--prefix", "/no/such/folder")
    assert code == 1 and out == "no results (is anything indexed? try `locus stats`)\n"


# --- sources ---------------------------------------------------------------------------------------------------------


def test_sources_json(world, capsys):
    code, out, err = run(capsys, "sources", "--json")
    payload = json.loads(out)
    assert (code, err) == (0, "")
    assert payload["schema"] == "locus.sources.v1" and payload["total"] == payload["indexed_total"] == 21
    text = asyncio.run(srv.locus_list_sources(limit=100))
    assert payload["paths"] == [line[2:] for line in text.splitlines() if line.startswith("- /")]


def test_sources_with_a_prefix_and_a_limit(world, capsys):
    folder = str(world.root / "engineering")
    code, out, _ = run(capsys, "sources", "--path-prefix", folder, "--limit", "2", "--json")
    payload = json.loads(out)
    assert code == 0 and len(payload["paths"]) == 2 and payload["total"] == 4 and payload["indexed_total"] == 21


def test_sources_text_prints_one_path_per_line_and_the_total_on_stderr(world, capsys):
    code, out, err = run(capsys, "sources", "--limit", "3")
    assert code == 0 and len(out.splitlines()) == 3 and all(line.startswith("/") for line in out.splitlines())
    assert "total: 21 file(s)" in err and "18 more" in err


# --- read ------------------------------------------------------------------------------------------------------------


def test_read_json_and_text(world, capsys):
    path = str(world.root / "pricing" / "pricing-v3.md")
    code, out, err = run(capsys, "read", path, "--json")
    payload = json.loads(out)
    assert (code, err) == (0, "") and payload["schema"] == "locus.read.v1" and payload["path"] == path
    assert payload["text"] and payload["content_notice"] == service.CONTENT_NOTICE
    code, out, err = run(capsys, "read", path)
    assert (code, out, err) == (0, payload["text"] + "\n", "")


def test_read_text_says_where_to_continue_on_stderr(world, capsys):
    path = str(world.root / "research" / "survey-results.csv")
    code, out, err = run(capsys, "read", path, "--max-chars", "200")
    assert code == 0 and len(out.rstrip("\n")) == 200 and "--start-char 200" in err


def test_read_refuses_a_path_that_is_not_indexed_with_exit_4(world, capsys, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("not in the index")
    for path in (
        str(outside),
        str(world.root / "pricing" / ".." / "pricing" / "pricing-v3.md"),
        "/etc/hosts",
        "pricing-v3.md",
    ):
        code, out, err = run(capsys, "read", path, "--json")
        error = json.loads(out)["error"]
        assert code == 4 and error["code"] == "not_indexed" and "not an indexed file" in error["message"]
        assert "not in the index" not in out  # the file's text is never in a refusal


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
def test_read_refuses_a_file_that_became_a_link_with_exit_4(tmp_path, monkeypatch, capsys):
    settings = Settings(
        data_dir=tmp_path / "data", collection="link", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, "link", embedder.name)
    root = (tmp_path / "notes").resolve()
    root.mkdir()
    (root / "a.md").write_text("an indexed note about zebras " * 5)
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    _wire(monkeypatch, embedder, store, settings)
    (tmp_path / "secret.md").write_text("OUTSIDE-SECRET")
    (root / "a.md").unlink()
    (root / "a.md").symlink_to(tmp_path / "secret.md")
    code, out, _ = run(capsys, "read", str(root / "a.md"), "--json")
    assert code == 4 and json.loads(out)["error"]["code"] == "confinement_refused" and "OUTSIDE-SECRET" not in out


def test_a_file_the_deny_list_excluded_is_not_in_the_index_so_read_refuses_it(tmp_path, monkeypatch, capsys):
    settings = Settings(
        data_dir=tmp_path / "data", collection="deny", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, "deny", embedder.name)
    root = (tmp_path / "notes").resolve()
    (root / "private").mkdir(parents=True)
    (root / "plan.md").write_text("the launch plan about zebras " * 5)
    (root / "passwords.txt").write_text("bank login: hunter2")  # the secret-file rule
    (root / "private" / "diary.md").write_text("a private diary entry " * 5)  # the policy file's [deny] paths
    home = tmp_path / "home"
    home.mkdir()
    matcher = matcher_for_folder(root, Policy(deny_paths=(str(root / "private"),)), home)
    index_folder(root, embedder, store, settings, log=lambda _m: None, matcher=matcher)
    _wire(monkeypatch, embedder, store, settings)
    assert {p.rsplit("/", 1)[1] for p in store.list_paths()} == {"plan.md"}
    for denied in ("passwords.txt", "private/diary.md"):
        code, out, _ = run(capsys, "read", str(root / denied), "--json")
        assert code == 4 and json.loads(out)["error"]["code"] == "not_indexed" and "hunter2" not in out
    code, out, _ = run(capsys, "read", str(root / "plan.md"), "--json")
    assert code == 0 and "launch plan" in json.loads(out)["text"]


# --- exit codes ------------------------------------------------------------------------------------------------------


def test_every_error_code_has_an_exit_code():
    assert set(cli.SERVICE_EXIT_CODES) == set(service.ERROR_CODES)
    assert set(cli.SERVICE_EXIT_CODES.values()) <= {1, 2, 3, 4, 5}


def test_a_usage_error_is_exit_2(world, capsys):
    code, out, err = run(capsys, "read")  # argparse: the path is missing
    assert code == 2 and out == "" and "usage:" in err
    code, out, _ = run(capsys, "query", "price", "-k", "0", "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "invalid_request"
    code, out, _ = run(capsys, "sources", "--limit", "5000", "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "invalid_request"
    code, out, _ = run(capsys, "read", "/x", "--max-chars", "5", "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "invalid_request"


def test_a_configuration_problem_is_exit_3(monkeypatch, capsys):
    def refuse(_settings):
        raise RuntimeError("VOYAGE_API_KEY is not set")

    monkeypatch.setattr(service, "open_index", refuse)
    for argv in (("query", "x", "--json"), ("sources", "--json"), ("read", "/x", "--json")):
        code, out, err = run(capsys, *argv)
        assert code == 3 and json.loads(out)["error"]["code"] == "not_configured" and err.startswith("error: ")


def test_an_empty_index_is_exit_3_for_query(tmp_path, monkeypatch, capsys):
    settings = Settings(
        data_dir=tmp_path / "data", collection="none", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    _wire(monkeypatch, embedder, ChromaStore(settings.data_dir, "none", embedder.name), settings)
    code, out, _ = run(capsys, "query", "anything", "--json")
    assert code == 3 and json.loads(out)["error"]["code"] == "no_index"
    code, out, _ = run(capsys, "sources", "--json")  # a list of nothing is still a true list
    assert code == 0 and json.loads(out)["total"] == 0


def test_an_embedder_failure_is_exit_5(demo_index, monkeypatch, capsys):
    class Rejecting(HashEmbedder):
        def embed_query(self, text):
            raise AuthFailed("key rejected")

    _wire(monkeypatch, Rejecting(), demo_index.store, demo_index.settings)
    code, out, _ = run(capsys, "query", "SECRET-QUERY", "--json")
    assert code == 5 and json.loads(out)["error"]["code"] == "embedder_error" and "SECRET-QUERY" not in out


def test_a_model_that_cannot_load_is_exit_5(monkeypatch, capsys):
    def refuse(_settings):
        raise service.LocalModelUnavailable("download failed")

    monkeypatch.setattr(service, "open_index", refuse)
    code, out, _ = run(capsys, "query", "x", "--json")
    assert code == 5 and json.loads(out)["error"]["code"] == "embedder_unavailable"


def test_an_unexpected_failure_is_exit_1_and_does_not_leak_its_message(world, monkeypatch, capsys):
    def boom(self, *a, **k):
        raise ValueError("bug while reading /private/diary.md")

    monkeypatch.setattr(service.Engine, "search", boom)
    code, out, err = run(capsys, "query", "x", "--json")
    payload = json.loads(out)
    assert code == 1 and payload["error"]["code"] == "internal_error"
    assert "/private/diary.md" not in out + err and "ValueError" in payload["error"]["message"]


def test_a_warning_goes_to_stderr_and_stdout_stays_json(demo_index, monkeypatch, capsys):
    monkeypatch.setattr(service, "get_settings", lambda: demo_index.settings)
    monkeypatch.setattr(service, "open_index", lambda _s: (demo_index.embedder, demo_index.store, "legacy collection"))
    monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
    code, out, err = run(capsys, "sources", "--json")
    assert code == 0 and json.loads(out)["total"] == 21
    assert "warning: legacy collection" in err and "legacy collection" not in out


@pytest.mark.parametrize("command", ["query", "sources", "read"])
def test_the_help_documents_the_exit_codes(command, capsys):
    with pytest.raises(SystemExit):
        cli.main([command, "--help"])
    text = capsys.readouterr().out
    for line in (
        "0  ok",
        "1  unexpected error",
        "2  usage error",
        "3  not configured",
        "4  refused",
        "5  embedder error",
    ):
        assert line in text
