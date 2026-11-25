"""Chunks with identical text collapse into one result row with `also_in`; every path is still shown. Offline."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as srv  # noqa: E402
from locus import service  # noqa: E402
from locus.collapse import collapse_duplicates  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore, Hit  # noqa: E402
from locus.service import Engine  # noqa: E402

from .demo_index import mcp_hits  # noqa: E402

BODY = "The pricing decision: three tiers, a yearly discount of twenty percent, and a free trial of fourteen days.\n"


def _setup(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data", collection="dups", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "files"
    for folder in ("project", "backup", "archive"):
        (root / folder).mkdir(parents=True)
        (root / folder / "pricing.md").write_text(BODY, encoding="utf-8")
    (root / "project" / "other.md").write_text("Hiring plan: two engineers in March, one designer in May.\n")
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
    monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
    return Engine(components=lambda: (embedder, store, settings)), root.resolve()


def test_three_copies_return_one_row_with_three_paths(tmp_path, monkeypatch):
    engine, root = _setup(tmp_path, monkeypatch)
    result = engine.search("pricing decision tiers yearly discount", k=5)
    rows = [r for r in result.results if r.path.endswith("pricing.md")]
    assert len(rows) == 1
    shown = {rows[0].path, *(d["path"] for d in rows[0].also_in or [])}
    assert all(d["chunk_index"] == 0 for d in rows[0].also_in)
    assert shown == {str(root / f / "pricing.md") for f in ("project", "backup", "archive")}
    assert all(r.chunk_index == 0 for r in rows)


def test_the_page_is_refilled_after_collapsing(tmp_path, monkeypatch):
    engine, _ = _setup(tmp_path, monkeypatch)
    result = engine.search("pricing decision tiers yearly discount", k=2)
    assert len(result.results) == 2  # one collapsed pricing row plus the next different chunk
    assert {r.path.rsplit("/", 1)[-1] for r in result.results} == {"pricing.md", "other.md"}


def test_json_keeps_its_schema_and_adds_only_an_optional_also_in(tmp_path, monkeypatch):
    engine, _ = _setup(tmp_path, monkeypatch)
    data = engine.search("pricing decision tiers yearly discount", k=5).to_dict()
    plain = [r for r in data["results"] if r["path"].endswith("other.md")]
    assert plain and "also_in" not in plain[0]
    assert set(plain[0]) == {"path", "chunk_index", "score", "modified", "page", "text", "truncated"}
    merged = [r for r in data["results"] if r["path"].endswith("pricing.md")][0]
    paths = [d["path"] for d in merged["also_in"]]
    assert sorted(paths) == sorted(set(paths)) and len(paths) == 2
    assert all(d["chunk_index"] == 0 for d in merged["also_in"]) and "also_in_more" not in merged
    json.dumps(data)


def test_mcp_text_lists_every_path(tmp_path, monkeypatch):
    _, root = _setup(tmp_path, monkeypatch)
    text = asyncio.run(srv.locus_search(query="pricing decision tiers yearly discount", top_k=5))
    assert text.count("also in:") == 1 and text.count("(chunk 0)") == 2
    for folder in ("project", "backup", "archive"):
        assert str(root / folder / "pricing.md") in text
    assert len(mcp_hits(text)) == 2


def test_collapse_helper_keeps_same_path_chunks_and_distinct_text():
    def hit(path, i, text, score=0.9):
        return Hit(path=path, chunk_index=i, text=text, score=score)

    rows = collapse_duplicates(
        [hit("/a", 0, "x  y"), hit("/b", 0, "x y"), hit("/a", 1, "x y"), hit("/c", 0, "z"), hit("/d", 0, "")]
    )
    assert [(r.path, r.chunk_index, r.also_in) for r in rows] == [
        ("/a", 0, (("/b", 0),)),
        ("/a", 1, ()),
        ("/c", 0, ()),
        ("/d", 0, ()),
    ]


def test_also_in_is_capped_with_a_count_and_every_entry_has_its_chunk_index():
    hits = [Hit(path=f"/f{n}", chunk_index=n, text="same words", score=0.9 - n / 100) for n in range(9)]
    (row,) = collapse_duplicates(hits)
    assert row.path == "/f0"
    assert row.also_in == tuple((f"/f{n}", n) for n in range(1, 6))  # the five best-ranked copies, with chunk numbers
    assert row.also_in_more == 3


def test_a_capped_row_says_how_many_more_in_the_mcp_text_and_json(tmp_path, monkeypatch):
    engine, _ = _setup(tmp_path, monkeypatch)
    root = tmp_path / "files"
    for n in range(8):
        (root / f"copy{n}").mkdir()
        (root / f"copy{n}" / "pricing.md").write_text(BODY, encoding="utf-8")
    from locus.ingest.indexer import index_folder as _index

    emb = HashEmbedder()
    store = ChromaStore(tmp_path / "data", "dups", emb.name)
    settings = Settings(
        data_dir=tmp_path / "data", collection="dups", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    _index(root, emb, store, settings, log=lambda _m: None)
    row = [r for r in engine.search("pricing decision tiers yearly discount", k=20).to_dict()["results"]][0]
    assert len(row["also_in"]) == 5 and row["also_in_more"] == 5  # 11 copies: one shown, five listed, five counted
    text = asyncio.run(srv.locus_search(query="pricing decision tiers yearly discount", top_k=20))
    assert "and 5 more" in text and text.count("(chunk 0)") == 5
