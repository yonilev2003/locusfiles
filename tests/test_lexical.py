"""Night 3 P3: the local lexical (BM25) leg prototype. Synthetic text only.

It is opt-in (LOCUS_LEXICAL), off by default; the store-level wiring is tested in test_hybrid_lexical.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.lexical import (
    LexicalIndex,
    heb_tokens,
    lexical_enabled,
    lexical_path,
    open_if_enabled,
    query_terms,
    rrf_fuse,
)


# -- tokenisation -------------------------------------------------------------------------------------------
def test_final_letters_are_normalised():
    assert heb_tokens("שלום עולם")[0] == "שלומ"
    assert "עולמ" in heb_tokens("שלום עולם")


def test_niqqud_is_removed_before_words_are_split():
    # a pointed word must stay one word (the splitter treats a combining mark as a separator)
    assert heb_tokens("שָׁלוֹם") == heb_tokens("שלום")


def test_prefix_letters_add_a_stripped_token():
    toks = heb_tokens("בשולחן")
    assert "בשולחנ" in toks and "שולחנ" in toks
    toks2 = heb_tokens("והשכר")  # ו + ה + שכר
    assert {"והשכר", "השכר", "שכר"} <= set(toks2)


def test_short_words_are_not_stripped():
    assert heb_tokens("בית") == ["בית"]  # three letters: a real word, not prefix + stub


def test_mixed_hebrew_and_english_terms_survive():
    assert set(query_terms("ב-minimum wage", "heb")) == {"minimum", "wage"}  # the lone prefix letter is dropped


def test_query_terms_drop_stop_words_and_duplicates():
    assert query_terms("what is the minimum wage wage", "uni") == ["minimum", "wage"]
    assert query_terms("מה הוא השכר", "heb")[0:1] == ["השכר"]


# -- the index ------------------------------------------------------------------------------------------------
CHUNKS = {
    "doc1:0": ("/n/doc1.md", "הנגריה מייצרת שולחנות עץ, ושאריות העץ נמכרות בחמישים שקל לקילו"),
    "doc1:1": ("/n/doc1.md", "Consumer surplus is the gap between willingness to pay and the price"),
    "doc2:0": ("/n/doc2.md", "ב-minimum wage studies, employment elasticity for teenagers was small"),
    "doc3:0": ("/n/doc3.md", "שָׁלוֹם עוֹלָם: a pointed greeting with niqqud"),
    "doc4:0": ("/n/sub/doc4.md", "Quarterly budget review and marketing spend"),
}


def _filled(tmp_path: Path, flavour: str = "heb") -> LexicalIndex:
    ix = LexicalIndex(tmp_path / "c.lexical.sqlite", flavour)
    ids = list(CHUNKS)
    ix.upsert(ids, [CHUNKS[i][0] for i in ids], [CHUNKS[i][1] for i in ids])
    return ix


def test_hebrew_prefix_matching(tmp_path):
    ix = _filled(tmp_path)
    assert ix.query("בשולחנות")[0].chunk_id == "doc1:0"  # query has the prefix, the text does not
    assert ix.query("שולחנות")[0].chunk_id == "doc1:0"
    assert ix.query("והשאריות")[0].chunk_id == "doc1:0"  # ו + ה + שאריות


def test_niqqud_in_text_and_query(tmp_path):
    ix = _filled(tmp_path)
    assert ix.query("שלום")[0].chunk_id == "doc3:0"
    assert ix.query("שָׁלוֹם")[0].chunk_id == "doc3:0"


def test_mixed_language_chunks_are_found_by_either_language(tmp_path):
    ix = _filled(tmp_path)
    assert ix.query("minimum wage elasticity")[0].chunk_id == "doc2:0"
    assert ix.query("willingness to pay price")[0].chunk_id == "doc1:1"


def test_scores_are_positive_and_sorted_best_first(tmp_path):
    ix = _filled(tmp_path)
    hits = ix.query("employment elasticity teenagers minimum wage")
    assert hits and all(h.score > 0 for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_no_usable_term_or_no_match_is_an_empty_list(tmp_path):
    ix = _filled(tmp_path)
    assert ix.query("the of and") == []
    assert ix.query("zzzzqqqq") == []
    assert ix.query('"') == []  # a bare quote must not become an FTS syntax error


def test_special_characters_in_a_query_do_not_break_fts(tmp_path):
    ix = _filled(tmp_path)
    assert isinstance(ix.query('wage" OR (budget* NEAR/2 x) -y:z'), list)


def test_upsert_replaces_instead_of_duplicating(tmp_path):
    ix = _filled(tmp_path)
    n = ix.count()
    ix.upsert(["doc4:0"], ["/n/sub/doc4.md"], ["Rewritten: gardening and compost"])
    assert ix.count() == n
    assert ix.query("budget marketing") == []
    assert ix.query("compost")[0].chunk_id == "doc4:0"


def test_delete_path_and_prefix_filter(tmp_path):
    ix = _filled(tmp_path)
    assert [h.chunk_id for h in ix.query("budget", path_prefix="/n/sub")] == ["doc4:0"]
    assert ix.query("budget", path_prefix="/n/subx") == []  # a folder boundary, not a string prefix
    assert ix.delete_path("/n/doc1.md") == 2
    assert ix.query("שולחנות") == [] and ix.count() == 3


def test_the_index_persists_and_keeps_its_tokenisation(tmp_path):
    ix = _filled(tmp_path)
    ix.close()
    again = LexicalIndex(tmp_path / "c.lexical.sqlite", "heb")
    assert again.count() == len(CHUNKS) and again.query("בשולחנות")[0].chunk_id == "doc1:0"
    again.close()
    with pytest.raises(RuntimeError, match="tokenisation"):
        LexicalIndex(tmp_path / "c.lexical.sqlite", "uni")


def test_uni_tokenisation_works_on_plain_text(tmp_path):
    ix = _filled(tmp_path, "uni")
    assert ix.query("consumer surplus")[0].chunk_id == "doc1:1"
    assert ix.query("שולחנות")[0].chunk_id == "doc1:0"  # exact word, no prefix logic needed
    assert ix.query("בשולחנות") == []  # the prefix-aware behaviour is what `heb` adds


def test_bad_flavour_and_mismatched_lengths(tmp_path):
    with pytest.raises(ValueError):
        LexicalIndex(tmp_path / "x.sqlite", "tri")
    ix = LexicalIndex(tmp_path / "y.sqlite")
    with pytest.raises(ValueError):
        ix.upsert(["a"], [], ["t"])


def test_the_file_is_private(tmp_path):
    ix = _filled(tmp_path)
    assert (ix.path.stat().st_mode & 0o077) == 0


# -- fusion --------------------------------------------------------------------------------------------------------
def test_rrf_matches_the_formula_and_is_deterministic():
    fused = rrf_fuse([["a", "b", "c"], ["c", "a", "d"]])
    assert fused == ["a", "c", "b", "d"]  # a: 1/61+1/62, c: 1/63+1/61, b: 1/62, d: 1/63
    assert rrf_fuse([["a", "b"], ["b", "a"]]) == ["a", "b"]  # a tie goes to the first-seen item
    assert rrf_fuse([]) == []
    assert rrf_fuse([["a"], ["b"]], weights=[1, 3]) == ["b", "a"]
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], weights=[1, 2])


def test_fusing_a_weak_dense_ranking_with_bm25_brings_the_lexical_hit_up(tmp_path):
    ix = _filled(tmp_path)
    dense = ["doc4:0", "doc1:1", "doc3:0", "doc2:0", "doc1:0"]  # a weak dense model: the right chunk is last
    bm25 = [h.chunk_id for h in ix.query("שולחנות עץ")]
    assert bm25[0] == "doc1:0"
    assert rrf_fuse([dense, bm25])[0] == "doc1:0"


# -- default behaviour is unchanged ----------------------------------------------------------------------------------
def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCUS_LEXICAL", raising=False)
    assert lexical_enabled() is False
    assert open_if_enabled(tmp_path, "locus") is None
    assert not lexical_path(tmp_path, "locus").exists()  # asking creates nothing on disk
    for on in ("1", "true", "YES", "on"):
        assert lexical_enabled({"LOCUS_LEXICAL": on}) is True
    for off in ("", "0", "false", "no", "off", "maybe"):
        assert lexical_enabled({"LOCUS_LEXICAL": off}) is False


def test_opt_in_opens_the_index_next_to_the_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_LEXICAL", "1")
    ix = open_if_enabled(tmp_path, "locus")
    assert ix is not None and ix.path == tmp_path / "locus.lexical.sqlite" and ix.path.exists()
    ix.close()


chromadb = pytest.importorskip("chromadb")

from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        collection="coll",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=100,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )


def test_indexing_and_search_with_the_variable_off_write_no_lexical_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCUS_LEXICAL", raising=False)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("The zebra crossing project was approved in March.")
    (notes / "b.md").write_text("Quarterly budget review. Marketing spend capped at 3k per month.")
    s = _settings(tmp_path)
    emb = HashEmbedder()
    store = ChromaStore(s.data_dir, s.collection, emb.name)
    index_folder(notes, emb, store, s, log=lambda _: None)
    assert store.count() == 2
    hits = store.query(emb.embed_query("zebra crossing approved"), k=2, query_text="zebra crossing approved")
    assert hits[0].path.endswith("a.md")
    assert not list(s.data_dir.glob("*.lexical.sqlite"))  # no second file appeared


def test_the_engine_does_not_import_the_lexical_module_while_the_variable_is_off():
    code = (
        "import sys, locus.mcp_server, locus.cli, locus.ingest.indexer, locus.ingest.store, locus.ingest.collection;"
        "sys.exit(1 if 'locus.ingest.lexical' in sys.modules else 0)"
    )
    env = {k: v for k, v in os.environ.items() if k != "LOCUS_LEXICAL"}
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, env=env).returncode == 0
