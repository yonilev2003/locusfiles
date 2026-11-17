"""service.py: the engine's operations as data. They must give the same paths, chunk indexes, totals and refusals as
the MCP tools, because both run the same steps. Offline: HashEmbedder, the demo vault, tmp folders."""

from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as srv  # noqa: E402
from locus import service  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.errors import RateLimited  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.service import Engine, ServiceError  # noqa: E402

from .demo_index import QUERIES, mcp_hits  # noqa: E402


def _wire(monkeypatch, embedder, store, settings) -> Engine:
    """One engine and the MCP server over the same index, with the freshness step switched off in both."""
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
    monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
    return Engine(components=lambda: (embedder, store, settings))


@pytest.fixture
def engine(demo_index, monkeypatch) -> Engine:
    return _wire(monkeypatch, demo_index.embedder, demo_index.store, demo_index.settings)


def _pairs(result) -> list[tuple[str, int]]:
    return [(h.path, h.chunk_index) for h in result.results]


# --- the same hits as locus_search ----------------------------------------------------------------------------------


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("k", [1, 3, 6, 12])
def test_search_returns_the_paths_and_chunk_indexes_locus_search_returns(engine, query, k):
    mcp = mcp_hits(asyncio.run(srv.locus_search(query=query, top_k=k)))
    got = engine.search(query, k=k)
    assert [(h["path"], h["chunk_index"]) for h in mcp] == _pairs(got) and len(mcp) == k
    for ours, theirs in zip(got.results, mcp, strict=True):
        assert abs(ours.score - theirs["score"]) < 6e-4  # the MCP header prints three decimals
        assert ours.modified == theirs["modified"]
        assert ours.text and ours.path.startswith("/") and ours.truncated is False


def test_search_inside_a_folder_matches_the_mcp_tool(engine, demo_index):
    folder = str(demo_index.root / "pricing")
    mcp = mcp_hits(asyncio.run(srv.locus_search(query="price", top_k=4, path_prefix=folder)))
    got = engine.search("price", k=4, path_prefix=folder)
    assert [(h["path"], h["chunk_index"]) for h in mcp] == _pairs(got) and got.results
    assert all(h.path.startswith(folder + "/") for h in got.results)


def test_a_date_window_with_no_query_lists_files_newest_first_like_the_mcp_tool(engine):
    mcp = mcp_hits(asyncio.run(srv.locus_search(query=None, top_k=5, modified_after="2000-01-01")))
    got = engine.search(None, k=5, modified_after="2000-01-01")
    assert [(h["path"], h["chunk_index"]) for h in mcp] == _pairs(got) and len(got.results) == 5
    assert got.order == "modified_desc" and all(h.score is None for h in got.results)
    assert got.more_files is True  # the vault holds 21 files
    assert [h.modified for h in got.results] == sorted((h.modified for h in got.results), reverse=True)


def test_a_window_that_holds_nothing_says_so(engine):
    got = engine.search(None, modified_after="2001-01-01", modified_before="2001-01-02")
    assert got.results == [] and "window" in got.note


def test_every_hit_carries_a_path_and_a_chunk_index_in_the_dict_too(engine):
    payload = engine.search(QUERIES[0]).to_dict()
    assert payload["schema"] == "locus.query.v1" and payload["content_notice"] == service.CONTENT_NOTICE
    assert payload["results"] and all(
        isinstance(r["path"], str) and isinstance(r["chunk_index"], int) for r in payload["results"]
    )


def test_max_chars_per_hit_cuts_the_text_and_says_so(engine):
    got = engine.search(QUERIES[0], k=3, max_chars_per_hit=40)
    assert all(len(h.text) <= 40 for h in got.results)
    assert any(h.truncated for h in got.results)


# --- bad requests ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # no query and no date
        {"query": ""},
        {"query": "x" * (service.MAX_QUERY_CHARS + 1)},
        {"query": "x", "k": 0},
        {"query": "x", "k": service.MAX_TOP_K + 1},
        {"query": "x", "modified_after": "not a date"},
        {"query": "x", "modified_after": "2026-02-01", "modified_before": "2026-01-01"},
        {"modified_after": "2026-02-01", "modified_before": "2026-02-01"},
    ],
)
def test_a_bad_search_is_an_invalid_request(engine, kwargs):
    with pytest.raises(ServiceError) as err:
        engine.search(**kwargs)
    assert err.value.code == "invalid_request"


def test_the_bounds_are_the_mcp_tools_bounds():
    assert service.DEFAULT_TOP_K == srv.SEARCH_DEFAULT_TOP_K
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    search = tools["locus_search"].input_schema["properties"]
    assert search["top_k"]["maximum"] == service.MAX_TOP_K and search["top_k"]["default"] == service.DEFAULT_TOP_K
    assert search["query"]["anyOf"][0]["maxLength"] == service.MAX_QUERY_CHARS
    sources = tools["locus_list_sources"].input_schema["properties"]["limit"]
    assert (sources["maximum"], sources["default"]) == (service.MAX_SOURCES_LIMIT, service.DEFAULT_SOURCES_LIMIT)
    read = tools["locus_read_file"].input_schema["properties"]["max_chars"]
    assert (read["minimum"], read["maximum"], read["default"]) == (
        service.MIN_READ_CHARS, service.MAX_READ_CHARS, service.DEFAULT_READ_CHARS,
    )  # fmt: skip


# --- what a failing embedder or an empty index says -----------------------------------------------------------------


def test_an_embedder_failure_is_a_typed_error_that_does_not_repeat_the_query(demo_index, monkeypatch):
    class Failing(HashEmbedder):
        def embed_query(self, text):
            raise RateLimited(f"slow down: {text}")

    engine = _wire(monkeypatch, Failing(), demo_index.store, demo_index.settings)
    with pytest.raises(ServiceError) as err:
        engine.search("SECRET-QUERY-WORDS")
    assert err.value.code == "embedder_error" and "SECRET-QUERY-WORDS" not in err.value.message


def test_a_store_failure_is_an_index_error(demo_index, monkeypatch):
    class Broken:
        def query(self, *a, **k):
            raise OSError("disk says no: /private/path")

    engine = _wire(monkeypatch, demo_index.embedder, Broken(), demo_index.settings)
    with pytest.raises(ServiceError) as err:
        engine.search("anything")
    assert err.value.code == "index_error" and "/private/path" not in err.value.message


def test_an_empty_index_is_no_index_not_no_match(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data", collection="empty", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    engine = _wire(monkeypatch, embedder, ChromaStore(settings.data_dir, "empty", embedder.name), settings)
    with pytest.raises(ServiceError) as err:
        engine.search("anything")
    assert err.value.code == "no_index"
    listing = engine.list_sources()
    assert (listing.total, listing.indexed_total, listing.paths) == (0, 0, []) and "Nothing is indexed" in listing.note


def test_a_folder_with_nothing_indexed_in_it_is_said_so(engine):
    got = engine.search("anything", path_prefix="/no/such/folder")
    assert got.results == [] and "inside that folder" in got.note


# --- the freshness step --------------------------------------------------------------------------------------------


def test_the_freshness_step_runs_with_the_index_and_its_note_is_passed_on(demo_index, monkeypatch):
    engine = _wire(monkeypatch, demo_index.embedder, demo_index.store, demo_index.settings)
    seen: list[tuple] = []

    def fake(embedder, store, settings, *rest):
        seen.append((embedder, store, settings))
        return "Freshness note: 3 changed file(s) not yet indexed."

    monkeypatch.setattr(service, "freshen_for_serve", fake)
    got = engine.search(QUERIES[0])
    assert got.freshness_note.startswith("Freshness note") and seen == [
        (demo_index.embedder, demo_index.store, demo_index.settings)
    ]
    assert engine.list_sources().freshness_note == got.freshness_note and len(seen) == 2
    assert engine.search(QUERIES[0], freshen=False).freshness_note is None and len(seen) == 2


def test_the_real_freshness_step_leaves_an_unchanged_vault_alone(demo_index):
    engine = Engine(components=lambda: (demo_index.embedder, demo_index.store, demo_index.settings))
    before = demo_index.store.count()
    got = engine.search(QUERIES[0])
    assert got.freshness_note is None and demo_index.store.count() == before


# --- list_sources ----------------------------------------------------------------------------------------------------


def test_list_sources_matches_the_mcp_tool(engine, demo_index):
    for prefix, limit in ((None, 100), (None, 5), (str(demo_index.root / "pricing"), 100), ("/no/such/folder", 10)):
        text = asyncio.run(srv.locus_list_sources(path_prefix=prefix, limit=limit))
        shown = [line[2:] for line in text.splitlines() if line.startswith("- /")]
        total = int(text.split("total: ", 1)[1].split(" ", 1)[0])
        got = engine.list_sources(prefix, limit)
        assert (got.paths, got.total) == (shown, total)
    everything = engine.list_sources(limit=1000)
    assert everything.total == everything.indexed_total == len(everything.paths) == 21
    assert everything.to_dict()["schema"] == "locus.sources.v1" and everything.embedder == demo_index.embedder.name


def test_list_sources_total_counts_past_the_limit(engine):
    got = engine.list_sources(limit=3)
    assert len(got.paths) == 3 and got.total == 21


def test_list_sources_uses_a_folder_boundary(engine, demo_index):
    assert engine.list_sources(str(demo_index.root / "pric")).total == 0


@pytest.mark.parametrize("limit", [0, service.MAX_SOURCES_LIMIT + 1])
def test_a_bad_limit_is_an_invalid_request(engine, limit):
    with pytest.raises(ServiceError) as err:
        engine.list_sources(limit=limit)
    assert err.value.code == "invalid_request"


# --- health ----------------------------------------------------------------------------------------------------------


def test_health(engine, demo_index):
    got = engine.health()
    assert (got.status, got.files, got.chunks) == ("ok", 21, demo_index.store.count())
    assert got.embedder == demo_index.embedder.name and got.collection == demo_index.settings.collection
    assert got.to_dict()["schema"] == "locus.health.v1"


# --- opening the index -----------------------------------------------------------------------------------------------


def test_a_configuration_problem_is_not_configured(monkeypatch):
    def refuse(_settings):
        raise RuntimeError("the collection was built with another embedder")

    monkeypatch.setattr(service, "open_index", refuse)
    with pytest.raises(ServiceError) as err:
        Engine().health()
    assert err.value.code == "not_configured" and "another embedder" in err.value.message


def test_a_model_that_cannot_load_is_embedder_unavailable(monkeypatch):
    def refuse(_settings):
        raise service.LocalModelUnavailable("download failed")

    monkeypatch.setattr(service, "open_index", refuse)
    with pytest.raises(ServiceError) as err:
        Engine().search("x")
    assert err.value.code == "embedder_unavailable"


def test_the_index_is_opened_once(demo_index):
    calls: list[int] = []

    def opener():
        calls.append(1)
        return demo_index.embedder, demo_index.store, demo_index.settings

    engine = Engine(components=opener)
    engine.warm_up()
    engine.health()
    engine.list_sources()
    assert len(calls) == 1


def test_a_warm_up_that_fails_does_not_raise(capsys):
    def opener():
        raise ServiceError("not_configured", "no")

    Engine(components=opener).warm_up()
    assert "warm-up did not finish" in capsys.readouterr().err


def test_codes_are_unique_and_listed():
    assert len(set(service.ERROR_CODES)) == len(service.ERROR_CODES)
    assert ServiceError("not_indexed", "x").to_dict() == {
        "schema": "locus.error.v1",
        "error": {"code": "not_indexed", "message": "x"},
    }


# --- read_file: the same confinement as locus_read_file -----------------------------------------------------------

SECRET = "OUTSIDE-THE-INDEX-SECRET-4711"


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data", collection="confine", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "corpus"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.md").write_text("indexed content about the zebra release " * 5)
    (root / "b.md").write_text("another indexed note about pricing " * 20)
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(SECRET)
    engine = _wire(monkeypatch, embedder, store, settings)
    return engine, root.resolve(), outside.resolve(), tmp_path


def _mcp_read(path) -> str:
    return asyncio.run(srv.locus_read_file(path=str(path)))


def test_an_indexed_file_is_read_like_the_mcp_tool_reads_it(corpus):
    engine, root, _outside, _tmp = corpus
    got = engine.read_file(str(root / "sub" / "a.md"))
    assert "indexed content about the zebra release" in got.text and got.more is False
    assert (got.start_char, got.end_char, got.total_chars) == (0, got.total_chars, len(got.text))
    assert got.text in _mcp_read(root / "sub" / "a.md")
    assert got.to_dict()["schema"] == "locus.read.v1" and got.to_dict()["content_notice"] == service.CONTENT_NOTICE


def test_a_long_file_is_read_in_bounded_pieces(corpus):
    engine, root, _outside, _tmp = corpus
    first = engine.read_file(str(root / "b.md"), max_chars=200)
    assert len(first.text) == 200 and first.more and first.next_start_char == 200
    rest = engine.read_file(str(root / "b.md"), start_char=first.next_start_char, max_chars=40000)
    assert first.text + rest.text == engine.read_file(str(root / "b.md"), max_chars=40000).text and not rest.more


@pytest.mark.parametrize(
    "spelling",
    [
        "{root}/sub/../b.md",  # dot-dot that lands on an indexed file: not the indexed spelling
        "{root}/./b.md",
        "{root}//b.md",
        "b.md",  # relative
        "{root}/sub/../../outside/secret.md",  # dot-dot out of the corpus
        "{tmp}/outside/secret.md",  # a real file the index does not list
        "{root}",  # a folder, not a file
        "{data}/chroma",  # Locus's own index folder
        "",
    ],
)
def test_other_spellings_and_other_places_are_refused_as_the_mcp_tool_refuses_them(corpus, spelling):
    engine, root, _outside, tmp = corpus
    path = spelling.format(root=root, tmp=tmp, data=tmp / "data")
    with pytest.raises(ServiceError) as err:
        engine.read_file(path)
    assert err.value.code == "not_indexed" and SECRET not in err.value.message
    assert "is not an indexed file" in asyncio.run(srv.locus_read_file(path=path))


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
class TestLinks:
    def test_a_file_replaced_by_a_symlink_is_refused(self, corpus):
        engine, root, outside, _tmp = corpus
        target = root / "sub" / "a.md"
        target.unlink()
        target.symlink_to(outside / "secret.md")
        with pytest.raises(ServiceError) as err:
            engine.read_file(str(target))
        assert err.value.code == "confinement_refused" and "now a symlink" in err.value.message
        assert f"Error: {err.value.message}" == _mcp_read(target)  # the same words as the MCP tool

    def test_a_folder_replaced_by_a_symlink_is_refused(self, corpus):
        engine, root, outside, _tmp = corpus
        (root / "sub" / "a.md").unlink()
        (root / "sub").rmdir()
        (root / "sub").symlink_to(outside)
        (outside / "a.md").write_text(SECRET)
        with pytest.raises(ServiceError) as err:
            engine.read_file(str(root / "sub" / "a.md"))
        assert err.value.code == "confinement_refused" and "resolves outside the indexed files" in err.value.message
        assert SECRET not in err.value.message

    def test_a_file_swapped_for_a_symlink_after_the_check_is_never_returned(self, corpus, monkeypatch):
        engine, root, outside, _tmp = corpus
        target = root / "sub" / "a.md"
        real_parse = service.parse_file

        def swapped_then_read(path):
            path.unlink()
            path.symlink_to(outside / "secret.md")  # the attacker wins the race right after the check
            return real_parse(path)

        monkeypatch.setattr(service, "parse_file", swapped_then_read)
        with pytest.raises(ServiceError) as err:
            engine.read_file(str(target))
        assert err.value.code == "confinement_refused" and "changed while it was being read" in err.value.message
        assert SECRET not in err.value.message


def test_a_file_deleted_after_indexing_is_unreadable_not_a_crash(corpus):
    engine, root, _outside, _tmp = corpus
    (root / "b.md").unlink()
    with pytest.raises(ServiceError) as err:
        engine.read_file(str(root / "b.md"))
    assert err.value.code == "file_unreadable"


@pytest.mark.parametrize("kwargs", [{"start_char": -1}, {"max_chars": 199}, {"max_chars": 40001}])
def test_bad_read_bounds_are_invalid_requests(corpus, kwargs):
    engine, root, _outside, _tmp = corpus
    with pytest.raises(ServiceError) as err:
        engine.read_file(str(root / "b.md"), **kwargs)
    assert err.value.code == "invalid_request"
