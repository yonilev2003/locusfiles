"""Text files in UTF-16 without a byte-order mark, cp1255 (Hebrew Windows) and cp1252 are read as text; bytes that make
no sense in any of them are skipped with a notice instead of being indexed as garbage. Offline."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.parsers import parse_file  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

HEBREW = "שלום עולם, זו הערה על תוכנית ההשקה והתקציב של הרבעון הבא. " * 3


def test_cp1255_hebrew_is_decoded_as_hebrew(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_bytes(HEBREW.encode("cp1255"))
    assert parse_file(path) == HEBREW


def test_cp1252_accents_are_decoded(tmp_path: Path):
    path = tmp_path / "note.txt"
    text = "Café, naïve façade, déjà vu: la réunion est à midi. " * 3
    path.write_bytes(text.encode("cp1252"))
    assert parse_file(path) == text


def test_utf16_without_a_byte_order_mark_is_decoded(tmp_path: Path):
    text = "Quarterly plan: ship the release in March. " * 4
    for codec in ("utf-16-le", "utf-16-be"):
        path = tmp_path / f"{codec}.txt"
        path.write_bytes(text.encode(codec))
        assert parse_file(path) == text


def test_bytes_that_are_no_text_in_any_encoding_are_not_read(tmp_path: Path):
    path = tmp_path / "blob.txt"
    path.write_bytes(bytes([0x81, 0x8D, 0x8F, 0x90, 0x9D, 0x01, 0x02, 0x03, 0x04, 0x05]) * 40)
    assert parse_file(path) is None


def test_a_file_with_unreadable_encoding_is_skipped_with_a_notice_and_named_in_the_report(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data", collection="enc", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "notes"
    root.mkdir()
    (root / "hebrew.txt").write_bytes(HEBREW.encode("cp1255"))
    (root / "junk.txt").write_bytes(bytes([0x81, 0x8D, 0x8F, 0x90, 0x9D, 0x01, 0x02, 0x03, 0x04, 0x05]) * 40)
    log: list[str] = []
    stats = index_folder(root, embedder, store, settings, log=log.append)
    assert [Path(p).name for p in store.list_paths()] == ["hebrew.txt"]
    assert stats.skipped_files["unreadable_encoding"] == [str(root / "junk.txt")]
    joined = "\n".join(log)
    assert "encoding Locus could not read safely" in joined and str(root / "junk.txt") in joined
    docs = store._col.get(include=["documents"])["documents"]
    assert any("שלום עולם" in d for d in docs)  # the Hebrew file is searchable text, not mojibake
    assert os.path.exists(root / "junk.txt")
