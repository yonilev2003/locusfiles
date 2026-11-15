"""A single file can produce more chunks than Chroma accepts in one call (a large index failed on two text
files of 12,544 and 14,345 chunks: "Batch size ... is greater than max batch size of 5461"). `ChromaStore.upsert` now
writes in slices and, if a later slice fails, removes the earlier ones so a file is never left half-indexed."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.ingest import store as store_module  # noqa: E402
from locus.ingest.store import MAX_UPSERT_BATCH, ChromaStore  # noqa: E402


def _rows(n: int, path: str = "/v/big.txt"):
    ids = [f"{path}:{i}" for i in range(n)]
    vectors = [[float(i % 7), 1.0, 0.0] for i in range(n)]
    docs = [f"chunk {i}" for i in range(n)]
    metas = [{"path": path, "chunk_index": i, "root": "/v"} for i in range(n)]
    return ids, vectors, docs, metas


def test_a_file_with_more_chunks_than_chroma_accepts_is_indexed(tmp_path: Path) -> None:
    store = ChromaStore(tmp_path / "data", "batch", "unit-3")
    n = 5461 + 2000  # above the limit that failed in production
    store.upsert(*_rows(n))
    assert store.count() == n


def test_a_failing_later_slice_leaves_no_half_indexed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ChromaStore(tmp_path / "data", "batch", "unit-3")
    monkeypatch.setattr(store_module, "MAX_UPSERT_BATCH", 100)
    real = store._col.upsert
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full")
        return real(**kwargs)

    monkeypatch.setattr(store._col, "upsert", flaky)
    with pytest.raises(RuntimeError):
        store.upsert(*_rows(350))
    assert store.count() == 0


def test_the_slice_size_stays_under_chroma_limit() -> None:
    assert MAX_UPSERT_BATCH < 5461
