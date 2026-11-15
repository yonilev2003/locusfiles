"""locus_search filters on the file's modified time, takes no query when a date
filter is set, starts every result with today's date and timezone, and shows each hit's modified time.

Every test uses the offline HashEmbedder and a Chroma store under tmp_path, with real files whose mtimes
are set by os.utime, so the filter runs against the metadata the indexer actually writes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from locus import mcp_server as srv

pytest.importorskip("chromadb")

JERUSALEM = timezone(timedelta(hours=3))  # 2026-10-22/23: IDT, before DST ends on 2026-10-25


@pytest.fixture(autouse=True)
def _fresh_state():
    srv._state.clear()
    yield
    srv._state.clear()


@pytest.fixture
def jerusalem(monkeypatch):
    """The process timezone the user would have; the filter's naive times mean this zone."""
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def _ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


FILES = {
    # name: (mtime, text). The first two edge files sit exactly on the window 2026-10-22 .. 2026-10-23.
    "edge_start.md": ("2026-10-22T00:00:00+03:00", "alpha pricing notes for the launch"),
    "late_evening.md": ("2026-10-22T23:59:59+03:00", "alpha pricing decision meeting"),
    "edge_end.md": ("2026-10-23T00:00:00+03:00", "alpha pricing follow up"),
    "day_before.md": ("2026-10-21T12:00:00+03:00", "alpha pricing early draft"),
}


def _index(tmp_path, files=FILES):
    from locus.config import DEFAULT_EXTENSIONS, Settings
    from locus.ingest.embedder import HashEmbedder
    from locus.ingest.indexer import index_folder
    from locus.ingest.store import ChromaStore

    root = tmp_path / "corpus"
    root.mkdir(exist_ok=True)
    for name, (when, text) in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        ts = _ts(when)
        os.utime(path, (ts, ts))
    settings = Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=800,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    index_folder(root, embedder, store, settings, log=lambda _: None)
    srv._state.update(embedder=embedder, store=store, settings=settings)
    return root


def _search(**kwargs) -> str:
    return asyncio.run(srv.locus_search(**kwargs))


def _paths(out: str) -> list[str]:
    return [m.group(1).rsplit("/", 1)[-1] for m in re.finditer(r"^## \d+\. (\S+)", out, re.MULTILINE)]


def _freeze_now(monkeypatch, iso="2026-10-23T09:30:00+03:00"):
    fixed = datetime.fromisoformat(iso)
    monkeypatch.setattr(srv, "_local_now", lambda: fixed)


def test_inclusive_after_and_exclusive_before_on_the_exact_edges(tmp_path, jerusalem):
    _index(tmp_path)
    out = _search(query="alpha pricing", modified_after="2026-10-22", modified_before="2026-10-23", top_k=10)
    assert sorted(_paths(out)) == ["edge_start.md", "late_evening.md"]  # start included, end excluded
    only_after = _search(query="alpha pricing", modified_after="2026-10-23", top_k=10)
    assert _paths(only_after) == ["edge_end.md"]
    only_before = _search(query="alpha pricing", modified_before="2026-10-22", top_k=10)
    assert _paths(only_before) == ["day_before.md"]


def test_a_naive_time_is_the_users_local_timezone_and_an_offset_wins(tmp_path, jerusalem, monkeypatch):
    _index(tmp_path)
    naive = _search(query="alpha pricing", modified_after="2026-10-22", modified_before="2026-10-23", top_k=10)
    assert sorted(_paths(naive)) == ["edge_start.md", "late_evening.md"]
    explicit = _search(
        query="alpha pricing",
        modified_after="2026-10-22T00:00:00+03:00",
        modified_before="2026-10-23T00:00:00+03:00",
        top_k=10,
    )
    assert _paths(explicit) == _paths(naive)
    # The same naive text in another zone is another window: UTC midnight is 03:00 in Jerusalem.
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    in_utc = _search(query="alpha pricing", modified_after="2026-10-22", modified_before="2026-10-23", top_k=10)
    assert sorted(_paths(in_utc)) == ["edge_end.md", "late_evening.md"]
    still_explicit = _search(
        query="alpha pricing",
        modified_after="2026-10-22T00:00:00+03:00",
        modified_before="2026-10-23T00:00:00+03:00",
        top_k=10,
    )
    assert sorted(_paths(still_explicit)) == sorted(_paths(naive))  # an explicit offset ignores TZ


def test_the_query_is_optional_with_a_date_filter_and_lists_newest_first(tmp_path, jerusalem):
    _index(tmp_path)
    out = _search(modified_after="2026-10-21", modified_before="2026-10-23")
    assert _paths(out) == ["late_evening.md", "edge_start.md", "day_before.md"]
    assert "newest first" in out
    assert "score" not in out  # nothing was ranked
    assert "Sources:" in out and "chunk 0" in out  # constraint 2: path + chunk_index on every hit


def test_no_query_and_no_filter_is_an_error_not_a_listing(tmp_path, jerusalem):
    _index(tmp_path)
    out = _search()
    assert out.startswith("Error:") and "modified_after" in out


def test_order_modified_desc_with_a_query_sorts_by_time_and_relevance_does_not(tmp_path, jerusalem):
    _index(tmp_path)
    by_time = _search(query="alpha pricing", order="modified_desc", top_k=10)
    assert _paths(by_time) == ["edge_end.md", "late_evening.md", "edge_start.md", "day_before.md"]
    by_relevance = _search(query="alpha pricing decision meeting", top_k=10)
    assert _paths(by_relevance)[0] == "late_evening.md"  # the exact phrase ranks first, whatever its age


def test_the_result_starts_with_today_and_the_filters_applied(tmp_path, jerusalem, monkeypatch):
    root = _index(tmp_path)
    _freeze_now(monkeypatch)
    out = _search(query="alpha pricing", modified_after="2026-10-22", modified_before="2026-10-23", top_k=10)
    lines = out.splitlines()
    assert lines[0] == "Today is 2026-10-23 (Asia/Jerusalem, UTC+03:00)."
    assert lines[1].startswith("Filters applied: modified on or after 2026-10-22T00:00:00+03:00; ")
    assert "modified before 2026-10-23T00:00:00+03:00" in lines[1]
    scoped = _search(query="alpha pricing", path_prefix=str(root), top_k=10)
    assert scoped.splitlines()[0] == "Today is 2026-10-23 (Asia/Jerusalem, UTC+03:00)."
    assert f"inside {root}" in scoped.splitlines()[1]
    plain = _search(query="alpha pricing", top_k=10)
    assert plain.splitlines()[0] == "Today is 2026-10-23 (Asia/Jerusalem, UTC+03:00)."
    assert "Filters applied" not in plain  # no filter, no filter line


def test_each_hit_shows_its_modified_time_in_local_time(tmp_path, jerusalem):
    _index(tmp_path)
    out = _search(query="alpha pricing decision meeting", top_k=10)
    assert "modified_at 2026-10-22T23:59:59+03:00" in out
    assert "modified_at 2026-10-21T12:00:00+03:00" in out


def test_an_empty_window_states_the_date_range_the_index_covers(tmp_path, jerusalem, monkeypatch):
    _index(tmp_path)
    _freeze_now(monkeypatch)
    out = _search(query="alpha pricing", modified_after="2027-01-01")
    assert out.splitlines()[0].startswith("Today is 2026-10-23")
    assert "No indexed file in the index was modified in that window" in out
    assert "between 2026-10-21T12:00:00+03:00 and 2026-10-23T00:00:00+03:00" in out
    assert "4 indexed file(s)" in out
    no_query = _search(modified_after="2027-01-01")
    assert "between 2026-10-21T12:00:00+03:00 and 2026-10-23T00:00:00+03:00" in no_query


def test_an_empty_window_with_a_path_prefix_reads_only_that_folder(tmp_path, jerusalem):
    root = _index(tmp_path, {**FILES, "elsewhere/other.md": ("2020-01-01T00:00:00+03:00", "alpha pricing other")})
    out = _search(query="alpha pricing", path_prefix=str(root / "elsewhere"), modified_after="2021-01-01")
    only = srv._iso_local(_ts("2020-01-01T00:00:00+03:00"))  # January: +02:00 in Jerusalem
    assert f"between {only} and {only}" in out and "1 indexed file(s)" in out


def test_bad_times_are_errors_the_model_can_read(tmp_path, jerusalem):
    _index(tmp_path)
    bad = _search(query="alpha", modified_after="yesterday")
    assert bad.startswith("Error: modified_after 'yesterday' is not an ISO 8601")
    swapped = _search(query="alpha", modified_after="2026-10-23", modified_before="2026-10-22")
    assert swapped.startswith("Error:") and "window is empty" in swapped
    same = _search(query="alpha", modified_after="2026-10-22", modified_before="2026-10-22")
    assert same.startswith("Error:") and "window is empty" in same
    assert _search(query="alpha", modified_before="  ").startswith("Error: modified_before is empty")


def test_a_chunk_without_a_recorded_time_never_passes_a_date_filter(tmp_path, jerusalem):
    from locus.ingest.embedder import HashEmbedder
    from locus.ingest.store import ChromaStore

    embedder = HashEmbedder()
    store = ChromaStore(tmp_path / "data", "test", embedder.name)
    vec = embedder.embed_documents(["old chunk"])[0]
    store.upsert(
        ids=["a", "b"],
        embeddings=[vec, vec],
        documents=["no time recorded", "has a time"],
        metadatas=[
            {"path": "/x/legacy.md", "chunk_index": 0},
            {"path": "/x/new.md", "chunk_index": 0, "mtime": _ts("2026-10-22T10:00:00+03:00")},
        ],
    )
    srv._state.update(embedder=embedder, store=store, settings=None)
    hits = store.query(vec, k=5, modified_after=_ts("2026-10-01T00:00:00+03:00"))
    assert [h.path for h in hits] == ["/x/new.md"]
    assert [h.path for h in store.files_modified(modified_after=_ts("2026-10-01T00:00:00+03:00"))] == ["/x/new.md"]
    assert len(store.query(vec, k=5)) == 2  # no filter: the legacy chunk is still searchable


def test_the_date_filter_combines_with_path_prefix(tmp_path, jerusalem):
    root = _index(tmp_path, {**FILES, "sub/inside.md": ("2026-10-22T10:00:00+03:00", "alpha pricing inside")})
    out = _search(query="alpha pricing", path_prefix=str(root / "sub"), modified_after="2026-10-22", top_k=10)
    assert _paths(out) == ["inside.md"]
    listing = _search(path_prefix=str(root / "sub"), modified_after="2026-10-22")
    assert _paths(listing) == ["inside.md"]


def test_the_tool_schema_makes_the_query_optional_and_stays_read_only():
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    search = tools["locus_search"]
    props = search.input_schema["properties"]
    assert {"query", "top_k", "path_prefix", "modified_after", "modified_before", "order"} <= set(props)
    assert "query" not in (search.input_schema.get("required") or [])
    assert props["order"]["enum"] == ["relevance", "modified_desc"]
    assert search.annotations.read_only_hint is True
    for name in ("query", "top_k", "path_prefix", "modified_after", "modified_before", "order"):
        assert props[name].get("description"), name  # Annotated[..., Field(description=...)] on every parameter


def test_the_ret_004_budget_is_unchanged():
    assert srv.SEARCH_CHAR_BUDGET == 19_500
    assert srv.SEARCH_DEFAULT_TOP_K == 6


class _WideStore:
    """25 full-size emoji hits whatever the filters, to size the relay payload with the new header."""

    embedder_name = "hash"

    def __init__(self):
        self.hits = [
            srv.Hit(f"/v/{i}.md", 0, "\U0001f600" * 3200, 0.9 - i / 100, mtime=_ts("2026-10-22T10:00:00+03:00"))
            for i in range(25)
        ]

    def query(self, embedding, k=5, path_prefix=None, **_filters):
        return self.hits[:k]

    def count(self):
        return 25


def test_the_relay_payload_with_filters_and_the_header_stays_under_200_kb(monkeypatch):
    class _Embed:
        name = "hash"

        def embed_query(self, text):
            return [0.0]

    srv._state.update(embedder=_Embed(), store=_WideStore(), settings=None)
    response = asyncio.run(
        srv._relay_dispatch(
            {
                "request_id": "r-time",
                "type": "call_tool",
                "tool": "locus_search",
                "arguments": {"query": "x", "top_k": 25, "modified_after": "2026-10-01", "order": "modified_desc"},
            },
            device="0a1b2c3d",
        )
    )
    assert response["ok"] is True, response
    text = response["result"]["content"][0]["text"]
    assert text.startswith("Today is ") and text.count("text not included") == 25 - 6
    assert len(json.dumps(response, ensure_ascii=False).encode("utf-8")) < 200_000
