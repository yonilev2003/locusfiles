"""Hybrid (BM25 + vector) search behind LOCUS_LEXICAL=1: off by default, fusion, rebuild, failure fallback, Hebrew.

Synthetic text and hand-made vectors only; offline (HashEmbedder where an embedder is needed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from locus.ingest import lexical
from locus.ingest.lexical import heb_tokens
from locus.ingest.store import ChromaStore, query_text_kwargs

pytest.importorskip("chromadb")

QUERY_VEC = [1.0, 0.0, 0.0, 0.0]
FAR = [0.0, 0.0, 1.0, 0.0]  # orthogonal to it: the vector ranking puts it last


def _store(tmp_path: Path) -> ChromaStore:
    return ChromaStore(tmp_path / "data", "coll", "hash-test")


def _meta(path: str, idx: int) -> dict:
    return {"path": path, "name": Path(path).name, "chunk_index": idx, "mtime": 1.0}


def _fill(store: ChromaStore, rare_text: str = "case number QX-48213 settled by Dana Weiss") -> None:
    """Ten filler chunks near the query vector and one chunk, far from it, holding the rare token."""
    ids = [f"f{i}:0" for i in range(10)] + ["t:0"]
    docs = [f"general filler note number {i} about the weekly plan" for i in range(10)] + [rare_text]
    store.upsert(
        ids=ids,
        embeddings=[[0.9 - 0.02 * i, 0.4, 0.0, 0.0] for i in range(10)] + [FAR],  # distinct: no ties
        documents=docs,
        metadatas=[_meta(f"/n/f{i}.md", 0) for i in range(10)] + [_meta("/n/target.md", 0)],
    )


# -- off by default -------------------------------------------------------------------------------------
def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCUS_LEXICAL", raising=False)
    assert query_text_kwargs("anything") == {}
    store = _store(tmp_path)
    _fill(store)
    assert not list((tmp_path / "data").glob("*.lexical.sqlite"))
    plain = store.query(QUERY_VEC, k=3)
    with_text = store.query(QUERY_VEC, k=3, query_text="QX-48213")
    assert plain == with_text  # the text is ignored while the leg is off
    assert all(h.path != "/n/target.md" for h in plain)


def test_on_kwargs_carry_the_text(monkeypatch):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    assert query_text_kwargs("QX-48213") == {"query_text": "QX-48213"}
    assert query_text_kwargs(None) == {}


# -- fusion ---------------------------------------------------------------------------------------------
def test_fusion_brings_the_rare_token_chunk_into_the_top_results(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    store = _store(tmp_path)
    _fill(store)
    vector_only = store.query(QUERY_VEC, k=3)
    assert all(h.path != "/n/target.md" for h in vector_only)
    hybrid = store.query(QUERY_VEC, k=3, query_text="QX-48213")
    assert hybrid[0].path == "/n/target.md"  # best in BM25, fused above the vector-only front-runner
    assert len(hybrid) == 3
    for h in hybrid:  # every result still carries a path and a chunk index
        assert h.path and isinstance(h.chunk_index, int) and h.text
    # a BM25-only chunk carries its true cosine to the query, not a made-up score
    assert hybrid[0].score == pytest.approx(0.0, abs=1e-3)


def test_fusion_keeps_vector_order_when_bm25_finds_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    store = _store(tmp_path)
    _fill(store)
    vector_only = store.query(QUERY_VEC, k=3)
    assert store.query(QUERY_VEC, k=3, query_text="zzzz-no-such-term") == vector_only


def test_fusion_respects_the_folder_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    store = _store(tmp_path)
    _fill(store)
    hits = store.query(QUERY_VEC, k=3, path_prefix="/n/f1.md", query_text="QX-48213")
    assert all(h.path == "/n/f1.md" for h in hits)


def test_rrf_order_is_the_formula():
    assert lexical.rrf_fuse([["a", "b", "c"], ["c", "a"]]) == ["a", "c", "b"]


def test_delete_path_removes_the_chunk_from_the_lexical_index(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    store = _store(tmp_path)
    _fill(store)
    store.delete_path("/n/target.md")
    hits = store.query(QUERY_VEC, k=3, query_text="QX-48213")
    assert all(h.path != "/n/target.md" for h in hits)


# -- rebuild --------------------------------------------------------------------------------------------
def test_rebuild_from_the_text_already_in_chroma(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCUS_LEXICAL", raising=False)
    store = _store(tmp_path)
    _fill(store)  # indexed with the leg off: no lexical data yet
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    assert store.query(QUERY_VEC, k=3, query_text="QX-48213")[0].path != "/n/target.md"  # nothing to fuse yet
    assert store.rebuild_lexical() == 11
    assert store.query(QUERY_VEC, k=3, query_text="QX-48213")[0].path == "/n/target.md"
    assert not list((tmp_path / "data").glob("*.tmp"))  # the swap left no temp file
    assert store.rebuild_lexical() == 11  # idempotent


def test_rebuild_flag_on_the_command_line(tmp_path, monkeypatch, capsys):
    from locus import cli
    from locus.ingest.embedder import HashEmbedder

    monkeypatch.delenv("LOCUS_LEXICAL", raising=False)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    monkeypatch.setenv("LOCUS_COLLECTION", "coll")
    emb = HashEmbedder()
    store = ChromaStore(tmp_path / "data", "coll", emb.name)
    store.upsert(
        ids=["a:0", "b:0"],
        embeddings=emb.embed_documents(["alpha note", "beta note"]),
        documents=["alpha note", "beta note"],
        metadatas=[_meta("/n/a.md", 0), _meta("/n/b.md", 0)],
    )
    with pytest.raises(SystemExit) as done:
        cli.main(["index", "--rebuild-lexical"])
    assert done.value.code == 0
    assert "2 chunks" in capsys.readouterr().out
    assert (tmp_path / "data" / "coll.lexical.sqlite").exists()


# -- failure fallback -----------------------------------------------------------------------------------
def test_a_failing_bm25_query_falls_back_to_vector_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    store = _store(tmp_path)
    _fill(store)
    expected = store.query(QUERY_VEC, k=3)

    def boom(self, *a, **kw):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(lexical.LexicalIndex, "query", boom)
    capsys.readouterr()
    assert store.query(QUERY_VEC, k=3, query_text="QX-48213") == expected
    out, err = capsys.readouterr()
    assert out == ""  # nothing on stdout (serve mode speaks MCP there)
    assert "lexical search failed" in err


def test_a_corrupt_lexical_file_never_fails_a_search_or_an_index_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    data = tmp_path / "data"
    data.mkdir()
    (data / "coll.lexical.sqlite").write_bytes(b"this is not a sqlite database" * 50)
    store = _store(tmp_path)
    _fill(store)  # the upsert must succeed although the lexical write cannot
    assert store.count() == 11
    hits = store.query(QUERY_VEC, k=3, query_text="QX-48213")
    assert len(hits) == 3 and all(h.path for h in hits)
    out, err = capsys.readouterr()
    assert out == ""
    assert "lexical" in err


def test_a_lexical_write_failure_does_not_fail_indexing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")

    def boom(self, *a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(lexical.LexicalIndex, "upsert", boom)
    store = _store(tmp_path)
    _fill(store)
    assert store.count() == 11
    assert "--rebuild-lexical" in capsys.readouterr().err


# -- Hebrew ---------------------------------------------------------------------------------------------
def test_hebrew_tokens_normalise_and_strip_prefixes():
    toks = heb_tokens("בשולחן הדיון של מנהלים")
    assert "בשולחנ" in toks and "שולחנ" in toks  # final nun normalised, prefix-stripped form added
    assert "מנהלימ" in toks  # final mem normalised
    assert "של" in toks


def test_hebrew_chunk_found_by_its_unprefixed_word(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    store = _store(tmp_path)
    _fill(store, rare_text="הפגישה התקיימה בשולחן העגול עם יעל כהן מתיק 7731/24")
    hits = store.query(QUERY_VEC, k=3, query_text="שולחן")
    assert hits[0].path == "/n/target.md"
    hits = store.query(QUERY_VEC, k=3, query_text="7731")
    assert hits[0].path == "/n/target.md"
