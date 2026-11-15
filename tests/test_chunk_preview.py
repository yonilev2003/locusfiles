"""`locus index --dry-run` and `locus estimate` report the chunk size the collection will really
use (LOCUS_CHUNK_TOKENS, the size the collection records, the legacy 700, or the window-aware default of a new
collection with a known local model), and never load a model or create a collection to find out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.chunk_policy import preview_chunk_tokens, settings_for_preview

chromadb = pytest.importorskip("chromadb")

from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402


def _settings(tmp_path: Path, **over) -> Settings:
    base = dict(
        data_dir=tmp_path / "data",
        collection="coll",
        embedder="local",
        voyage_model="",
        openai_model="",
        local_model="intfloat/multilingual-e5-small",
        chunk_tokens=700,
        overlap_tokens=100,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )
    base.update(over)
    return Settings(**base)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("intfloat/multilingual-e5-small", 300),
        ("BAAI/bge-small-en-v1.5", 300),
        ("/models/multilingual-e5-base/", 300),
        ("BAAI/bge-m3", 700),
        ("some-org/unknown-model", 700),
    ],
)
def test_a_new_collection_previews_the_window_aware_default_for_known_local_models(tmp_path, model, expected):
    assert preview_chunk_tokens(_settings(tmp_path, local_model=model))[0] == expected


def test_hosted_and_hash_embedders_preview_700(tmp_path):
    for kind in ("managed", "voyage", "openai", "hash"):
        assert preview_chunk_tokens(_settings(tmp_path, embedder=kind))[0] == 700


def test_the_override_wins(tmp_path):
    s = _settings(tmp_path, chunk_tokens=450, chunk_tokens_explicit=True)
    assert preview_chunk_tokens(s) == (450, "LOCUS_CHUNK_TOKENS")


def test_a_recorded_size_is_previewed_and_a_populated_unrecorded_collection_stays_700(tmp_path):
    s = _settings(tmp_path)
    recorded = ChromaStore(s.data_dir, "coll", "local:x", chunk_tokens=300)
    assert preview_chunk_tokens(s, recorded) == (300, "recorded by the existing collection")

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("some text to index")
    old = ChromaStore(s.data_dir, "oldcoll", "hash-256")
    index_folder(notes, HashEmbedder(), old, _settings(tmp_path, embedder="hash", chunk_tokens=100), log=lambda _: None)
    n, why = preview_chunk_tokens(s, old)
    assert n == 700 and "legacy" in why

    empty = ChromaStore(s.data_dir, "emptycoll", "local:x")  # created empty: the next open records the new default
    assert preview_chunk_tokens(s, empty)[0] == 300


def test_settings_for_preview_carries_the_size_into_the_estimator_settings(tmp_path):
    s, line = settings_for_preview(_settings(tmp_path))
    assert s.chunk_tokens == 300 and "300 tokens" in line and "new collection" in line


@pytest.fixture()
def corpus(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("alpha beta gamma " * 50)
    return root


def _run(cli, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code


def _env(monkeypatch, tmp_path, **env):
    from locus import cli

    monkeypatch.setattr("locus.config.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("locus.config.find_dotenv", lambda *a, **k: "")
    for k in ("LOCUS_CHUNK_TOKENS", "LOCUS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "local")
    monkeypatch.setenv("LOCUS_LOCAL_MODEL", "intfloat/multilingual-e5-small")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cli.get_settings.cache_clear()
    return cli


def test_estimate_and_dry_run_report_300_for_a_new_local_collection_without_loading_a_model(
    tmp_path, corpus, monkeypatch, capsys
):
    cli = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "locus.ingest.embedder.LocalEmbedder.__init__", lambda *a, **k: pytest.fail("a model was loaded")
    )
    try:
        assert _run(cli, ["estimate", str(corpus)]) == 0
        assert (
            "Chunk size used: 300 tokens (default for a new collection with a 512-token model)."
            in capsys.readouterr().out
        )
        assert _run(cli, ["estimate", str(corpus), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["chunk_tokens"] == 300
        assert _run(cli, ["index", str(corpus), "--dry-run"]) == 0
        assert "Chunk size used: 300 tokens" in capsys.readouterr().out
    finally:
        cli.get_settings.cache_clear()
    assert not (tmp_path / "data").exists()  # nothing was created


def test_estimate_reports_the_recorded_size_of_an_existing_collection(tmp_path, corpus, monkeypatch, capsys):
    cli = _env(monkeypatch, tmp_path, LOCUS_COLLECTION="coll")
    ChromaStore(tmp_path / "data", "coll", "local:intfloat/multilingual-e5-small", chunk_tokens=300)
    try:
        assert _run(cli, ["estimate", str(corpus)]) == 0
    finally:
        cli.get_settings.cache_clear()
    assert "Chunk size used: 300 tokens (recorded by the existing collection)." in capsys.readouterr().out


def test_estimate_reports_700_for_a_populated_legacy_collection_and_the_override(tmp_path, corpus, monkeypatch, capsys):
    cli = _env(monkeypatch, tmp_path, LOCUS_COLLECTION="coll")
    legacy = ChromaStore(tmp_path / "data", "coll", "local:intfloat/multilingual-e5-small")
    index_folder(
        corpus, HashEmbedder(), legacy, _settings(tmp_path, embedder="hash", chunk_tokens=100), log=lambda _: None
    )
    del legacy
    try:
        assert _run(cli, ["estimate", str(corpus)]) == 0
        assert "Chunk size used: 700 tokens (legacy default" in capsys.readouterr().out
        monkeypatch.setenv("LOCUS_CHUNK_TOKENS", "500")
        cli.get_settings.cache_clear()
        assert _run(cli, ["estimate", str(corpus)]) == 0
        assert "Chunk size used: 500 tokens (LOCUS_CHUNK_TOKENS)." in capsys.readouterr().out
    finally:
        cli.get_settings.cache_clear()
