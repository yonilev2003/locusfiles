"""The default chunk size follows the embedding model's window, a collection records
the size it was created with (an older one keeps the legacy 700), and the English BGE query instruction
goes only to English bge-*-en models.

Offline: HashEmbedder with a fake `window_tokens`; no model is loaded.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.chunk_policy import (
    CHUNK_TOKENS_KEY,
    LEGACY_CHUNK_TOKENS,
    WINDOW_AWARE_CHUNK_TOKENS,
    chunk_tokens_for_new_collection,
    effective_chunk_tokens,
    window_aware_chunk_tokens,
)
from locus.ingest.embedder import HashEmbedder, uses_english_bge_query_instruction

chromadb = pytest.importorskip("chromadb")

from locus.ingest.collection import open_index  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402


def _settings(tmp_path: Path, **over) -> Settings:
    base = Settings(
        data_dir=tmp_path / "data",
        collection="coll",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=LEGACY_CHUNK_TOKENS,
        overlap_tokens=100,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )
    return replace(base, **over)


class _Windowed(HashEmbedder):
    """The hash embedder claiming a model window (a fake: nothing is loaded)."""

    def __init__(self, window: int | None, name: str = "hash:windowed") -> None:
        super().__init__()
        self.window_tokens = window
        self.name = name


def _long_note(folder: Path) -> Path:
    folder.mkdir(exist_ok=True)
    # ~2,800 words of distinct short paragraphs: far more than one 300-token chunk, a few 700-token ones.
    paras = [f"Paragraph {i}: " + " ".join(f"w{i}x{j}" for j in range(25)) for i in range(110)]
    (folder / "long.md").write_text("\n\n".join(paras), encoding="utf-8")
    return folder


# -- the window decides the default ------------------------------------------------------------------
@pytest.mark.parametrize("window", [128, 256, 512])
def test_small_window_models_default_to_300(window):
    assert window_aware_chunk_tokens(_Windowed(window)) == WINDOW_AWARE_CHUNK_TOKENS == 300


@pytest.mark.parametrize("window", [513, 8192, None, 0])
def test_large_unknown_or_missing_window_keeps_the_legacy_size(window):
    assert window_aware_chunk_tokens(_Windowed(window)) is None


def test_an_embedder_without_a_window_attribute_keeps_the_legacy_size():
    assert window_aware_chunk_tokens(HashEmbedder()) is None


def test_explicit_chunk_tokens_wins_when_a_collection_is_created(tmp_path):
    s = _settings(tmp_path, chunk_tokens=450, chunk_tokens_explicit=True)
    assert chunk_tokens_for_new_collection(s, _Windowed(512)) == 450
    assert chunk_tokens_for_new_collection(s, _Windowed(None)) == 450


# -- a new collection records it; the indexer cuts with it ----------------------------------------------
def test_new_small_window_collection_records_300_and_indexes_with_it(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    emb = _Windowed(512)
    monkeypatch.setattr("locus.ingest.collection.get_embedder", lambda _s: emb)
    _, store, _ = open_index(s)
    assert store.chunk_tokens == 300
    assert store._col.metadata[CHUNK_TOKENS_KEY] == 300
    assert effective_chunk_tokens(s, store) == 300

    notes = _long_note(tmp_path / "notes")
    index_folder(notes, emb, store, s, log=lambda _: None)
    small = store.count()

    # the same file in a fresh legacy collection (no window) is cut into fewer, longer chunks
    s2 = _settings(tmp_path, collection="legacy")
    emb2 = _Windowed(None, name="hash:legacy")
    monkeypatch.setattr("locus.ingest.collection.get_embedder", lambda _s: emb2)
    _, store2, _ = open_index(s2)
    assert store2.chunk_tokens is None
    assert effective_chunk_tokens(s2, store2) == LEGACY_CHUNK_TOKENS
    index_folder(notes, emb2, store2, s2, log=lambda _: None)
    assert small > store2.count() > 0


def test_reopening_reads_the_recorded_size_back(tmp_path):
    s = _settings(tmp_path)
    first = ChromaStore(s.data_dir, "coll", "hash:windowed", chunk_tokens=300)
    assert first.chunk_tokens == 300
    again = ChromaStore(s.data_dir, "coll", "hash:windowed")  # no chunk_tokens passed: reads what is there
    assert again.chunk_tokens == 300


def test_hosted_style_embedder_records_nothing(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr("locus.ingest.collection.get_embedder", lambda _s: _Windowed(None))
    _, store, _ = open_index(s)
    assert store.chunk_tokens is None
    assert CHUNK_TOKENS_KEY not in (store._col.metadata or {})


# -- existing collections must not change silently --------------------------------------------------------
def test_a_populated_collection_without_a_record_keeps_700_after_upgrade(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    notes = _long_note(tmp_path / "notes")
    # built "before chunk sizes were recorded": no chunk_tokens passed, one file indexed
    old = ChromaStore(s.data_dir, "coll", "hash:windowed")
    index_folder(notes, _Windowed(512), old, s, log=lambda _: None)
    assert old.count() > 0
    del old

    # the upgraded engine opens it with a 512-window model: it must NOT start cutting 300-token chunks
    monkeypatch.setattr("locus.ingest.collection.get_embedder", lambda _s: _Windowed(512))
    _, store, _ = open_index(s)
    assert store.chunk_tokens is None
    assert CHUNK_TOKENS_KEY not in (store._col.metadata or {})
    assert effective_chunk_tokens(s, store) == LEGACY_CHUNK_TOKENS


def test_an_empty_collection_without_a_record_gets_it_on_first_open(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    ChromaStore(s.data_dir, "coll", "hash:windowed")  # created empty, e.g. by an older `locus serve`
    monkeypatch.setattr("locus.ingest.collection.get_embedder", lambda _s: _Windowed(512))
    _, store, _ = open_index(s)
    assert store.chunk_tokens == 300


def test_locus_chunk_tokens_overrides_a_recorded_size(tmp_path):
    s = _settings(tmp_path, chunk_tokens=500, chunk_tokens_explicit=True)
    store = ChromaStore(s.data_dir, "coll", "hash:windowed", chunk_tokens=300)
    assert store.chunk_tokens == 300
    assert effective_chunk_tokens(s, store) == 500


def test_a_store_without_the_attribute_falls_back_to_settings(tmp_path):
    class Bare:  # test doubles for VectorStore have no chunk_tokens
        pass

    assert effective_chunk_tokens(_settings(tmp_path, chunk_tokens=123), Bare()) == 123


def test_get_settings_marks_the_override_only_when_the_variable_is_set(monkeypatch):
    from locus.config import get_settings

    monkeypatch.setattr("locus.config.load_dotenv", lambda *a, **k: False)  # hermetic: no .env from a parent folder
    monkeypatch.setattr("locus.config.find_dotenv", lambda *a, **k: "")
    get_settings.cache_clear()
    monkeypatch.delenv("LOCUS_CHUNK_TOKENS", raising=False)
    try:
        s = get_settings()
        assert (s.chunk_tokens, s.chunk_tokens_explicit) == (700, False)
        get_settings.cache_clear()
        monkeypatch.setenv("LOCUS_CHUNK_TOKENS", "400")
        s = get_settings()
        assert (s.chunk_tokens, s.chunk_tokens_explicit) == (400, True)
    finally:
        get_settings.cache_clear()


# -- item 2: the English BGE query instruction ----------------------------------------------------------------
@pytest.mark.parametrize(
    "model",
    [
        "BAAI/bge-small-en-v1.5",
        "BAAI/bge-base-en-v1.5",
        "BAAI/bge-large-en-v1.5",
        "BAAI/bge-small-en",
        "bge-small-en-v1.5",
        "/models/bge-small-en-v1.5/",
    ],
)
def test_english_bge_models_get_the_instruction(model):
    assert uses_english_bge_query_instruction(model)


@pytest.mark.parametrize(
    "model",
    [
        "BAAI/bge-m3",
        "BAAI/bge-small-zh-v1.5",
        "BAAI/bge-reranker-v2-m3",
        "intfloat/multilingual-e5-small",
        "intfloat/multilingual-e5-base",
        "sentence-transformers/all-MiniLM-L6-v2",
        "my-org/bgevision-en",
    ],
)
def test_other_models_do_not(model):
    assert not uses_english_bge_query_instruction(model)


def test_policy_and_chunk_size_both_recorded_on_an_empty_older_collection(tmp_path):
    """Two metadata records on one first open: the second must not drop the first."""
    from locus.ingest.store import INPUT_TYPE_POLICY_KEY

    s = _settings(tmp_path)
    ChromaStore(s.data_dir, "coll", "hash:windowed")  # empty, records neither
    store = ChromaStore(s.data_dir, "coll", "hash:windowed", input_type_policy="v1", chunk_tokens=300)
    meta = dict(store._col.metadata)
    assert meta[INPUT_TYPE_POLICY_KEY] == "v1" and meta[CHUNK_TOKENS_KEY] == 300
    assert (store.input_type_policy, store.chunk_tokens) == ("v1", 300)
    reopened = ChromaStore(s.data_dir, "coll", "hash:windowed")
    assert (reopened.input_type_policy, reopened.chunk_tokens) == ("v1", 300)
