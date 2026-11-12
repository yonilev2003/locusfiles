"""Night 2 (T2, 2026-10-25): a file over LOCUS_MAX_FILE_MB used to be skipped without a word by a real
`locus index` run (only `--dry-run` counted it, without naming it). A lecture deck full of pictures is
easily over the default 25 MB, so the run now names each such file and says how to include it."""

from __future__ import annotations

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.embedder import HashEmbedder
from locus.ingest.indexer import index_folder
from locus.ingest.store import ChromaStore

pytest.importorskip("chromadb")


def _settings(tmp_path, max_file_mb: float) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=800,
        overlap_tokens=10,
        max_file_mb=max_file_mb,
        extensions=DEFAULT_EXTENSIONS,
    )


def test_an_oversized_file_is_named_in_the_run_log(tmp_path):
    root = tmp_path / "course"
    root.mkdir()
    (root / "small.md").write_text("small file with words\n", encoding="utf-8")
    (root / "big-deck.md").write_text("word " * 400_000, encoding="utf-8")  # about 2 MB
    settings = _settings(tmp_path, max_file_mb=1)
    messages: list[str] = []
    store = ChromaStore(settings.data_dir, "test", HashEmbedder().name)
    stats = index_folder(root, HashEmbedder(), store, settings, log=messages.append)
    notices = [m for m in messages if "big-deck.md" in m and "LOCUS_MAX_FILE_MB" in m]
    assert len(notices) == 1 and "1 MB" in notices[0]
    assert [p for p in store.list_paths() if "big-deck" in p] == []
    assert stats.files_indexed == 1 and stats.files_seen == 1  # counters and summary line are unchanged


def test_no_notice_when_nothing_is_over_the_limit(tmp_path):
    root = tmp_path / "course"
    root.mkdir()
    (root / "small.md").write_text("small file with words\n", encoding="utf-8")
    settings = _settings(tmp_path, max_file_mb=1)
    messages: list[str] = []
    store = ChromaStore(settings.data_dir, "test", HashEmbedder().name)
    index_folder(root, HashEmbedder(), store, settings, log=messages.append)
    assert not [m for m in messages if "LOCUS_MAX_FILE_MB" in m]
