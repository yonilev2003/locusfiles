"""Base64 noise: payloads in notebooks and markdown never reach the index, and rows written by an older parser
are re-indexed once. Offline, HashEmbedder; payloads are generated, not stored."""

from __future__ import annotations

import base64
import json
import os
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import IndexStats, index_file  # noqa: E402
from locus.ingest.noise import PARSER_VERSION, REMOVED, is_noise_chunk, strip_base64  # noqa: E402
from locus.ingest.parsers import parse_file  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

PAYLOAD = base64.b64encode(os.urandom(3000)).decode()  # about 4000 characters, one line
WRAPPED = textwrap.fill(PAYLOAD, 76)  # the same payload wrapped like MIME: no line is 200 characters


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", collection="noise", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip


def _store(tmp_path: Path) -> tuple[ChromaStore, HashEmbedder, Settings]:
    settings, embedder = _settings(tmp_path), HashEmbedder()
    return ChromaStore(settings.data_dir, settings.collection, embedder.name), embedder, settings


def _notebook(path: Path) -> Path:
    cells = [
        {
            "cell_type": "markdown",
            "source": [
                f"# Quarterly churn model\n\nThe attached chart: ![chart](attachment:c.png)\n\n"
                f"data:image/png;base64,{PAYLOAD}\n"
            ],
        },
        {"cell_type": "code", "source": [f'image = "{PAYLOAD}"\nprint("churn drivers are pricing and onboarding")']},
    ]
    path.write_text(json.dumps({"cells": cells, "metadata": {}, "nbformat": 4}), encoding="utf-8")
    return path


def test_strip_base64_removes_data_uri_and_long_run_but_keeps_prose():
    text = f"Intro line.\n![x](data:image/png;base64,{PAYLOAD})\nkey = '{PAYLOAD}'\nOutro line."
    out = strip_base64(text)
    assert PAYLOAD[:50] not in out
    assert out.count(REMOVED) == 2
    assert "Intro line." in out and "Outro line." in out


@pytest.mark.parametrize("separator", ["=" * 300, "/" * 300, "a" * 300, "-" * 300])
def test_strip_base64_keeps_separators_and_repeated_characters(separator: str):
    assert strip_base64(f"head\n{separator}\ntail") == f"head\n{separator}\ntail"


def test_is_noise_chunk_catches_wrapped_base64_and_spares_prose():
    assert is_noise_chunk(WRAPPED)
    assert not is_noise_chunk("We decided on three pricing tiers and a yearly discount. " * 20)
    mixed = "Real paragraph about the launch plan and its risks. " * 10 + "\n" + WRAPPED[:200]
    assert not is_noise_chunk(mixed)


def test_notebook_text_has_no_payload_in_markdown_or_code_cell(tmp_path: Path):
    text = parse_file(_notebook(tmp_path / "a.ipynb")) or ""
    assert PAYLOAD[:50] not in text
    assert "Quarterly churn model" in text and "pricing and onboarding" in text


def test_notebook_index_holds_no_payload_and_is_still_searchable(tmp_path: Path):
    store, embedder, settings = _store(tmp_path)
    nb = _notebook(tmp_path / "a.ipynb")
    stats = IndexStats()
    assert index_file(nb, tmp_path, embedder, store, settings, stats, log=lambda _m: None) > 0
    got = store._col.get(include=["documents", "metadatas"])
    assert got["documents"] and all(PAYLOAD[:50] not in d for d in got["documents"])
    assert any("churn" in d for d in got["documents"])
    assert all(m["parser_version"] == PARSER_VERSION for m in got["metadatas"])


def test_wrapped_base64_in_markdown_attachment_is_dropped_whole_chunks(tmp_path: Path):
    store, embedder, settings = _store(tmp_path)
    note = tmp_path / "n.md"
    note.write_text("# Notes\n\nShort intro about the project.\n\n" + ("\n\n".join([WRAPPED] * 6)), encoding="utf-8")
    index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=lambda _m: None)
    docs = store._col.get(include=["documents"])["documents"]
    assert docs and not any(is_noise_chunk(d) for d in docs)
    assert any("Short intro" in d for d in docs)
    got = store._col.get(include=["metadatas"])["metadatas"]
    assert sorted(m["chunk_index"] for m in got) == list(range(len(got)))  # no gaps after the filter


def test_old_row_is_reindexed_once_even_if_the_file_is_unchanged(tmp_path: Path):
    store, embedder, settings = _store(tmp_path)
    note = tmp_path / "n.md"
    note.write_text("# Plan\n\nPricing decision and launch risks.\n", encoding="utf-8")
    log = lambda _m: None  # noqa: E731
    assert index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=log) > 0
    assert store.parser_version(str(note)) == PARSER_VERSION
    assert index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=log) == 0  # unchanged: skipped

    got = store._col.get(include=["metadatas", "documents", "embeddings"])
    old = [{k: v for k, v in m.items() if k != "parser_version"} for m in got["metadatas"]]
    store._col.delete(ids=got["ids"])  # a pre-stamp row: same chunk, no parser_version key at all
    store._col.upsert(ids=got["ids"], embeddings=got["embeddings"], documents=got["documents"], metadatas=old)
    assert store.parser_version(str(note)) == 1
    assert index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=log) > 0
    assert store.parser_version(str(note)) == PARSER_VERSION
    assert index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=log) == 0
