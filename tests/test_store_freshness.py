"""A long-lived reader (`locus serve`, `locus watch`) must see index writes made by another process.

Chroma keeps a collection's vector index in memory once a client opens it, so before the fix a
running `locus serve` kept ranking by the vectors it loaded at startup while a separate
`locus index` changed the index: new files never came back, an edited chunk kept its old vector,
and after `--prune` a search whose stale top-k held a deleted chunk crashed. These tests write
from a real second process, because a second store in the same process shares Chroma's in-memory
system and would hide the bug.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import locus

pytest.importorskip("chromadb")

from locus.ingest.store import ChromaStore  # noqa: E402

COLLECTION = "freshness"
DIM = 8

_WRITER = """
import json, sys
from pathlib import Path
from locus.ingest.store import ChromaStore
data_dir, embedder, ops = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
store = ChromaStore(Path(data_dir), "freshness", embedder)
for op in ops:
    if op["op"] == "upsert":
        store.upsert([op["id"]], [op["vec"]], [op["text"]], [{"path": op["path"], "chunk_index": 0}])
    else:
        store.delete_path(op["path"])
"""


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def _upsert(path: str, i: int, text: str) -> dict:
    return {"op": "upsert", "id": path, "vec": _vec(i), "text": text, "path": path}


def _write_from_another_process(data_dir: Path, *ops: dict, embedder: str = "unit-8") -> None:
    env = {**os.environ, "PYTHONPATH": str(Path(locus.__file__).resolve().parents[1])}
    proc = subprocess.run(
        [sys.executable, "-c", _WRITER, str(data_dir), embedder, json.dumps(list(ops))],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_reader_sees_add_edit_and_prune_from_another_process(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_from_another_process(data_dir, _upsert("/v/a.md", 0, "alpha"), _upsert("/v/b.md", 1, "beta"))
    reader = ChromaStore(data_dir, COLLECTION, "unit-8")  # the long-lived `locus serve` side
    assert [h.path for h in reader.query(_vec(0), k=1)] == ["/v/a.md"]

    # A file indexed after the reader opened is searchable.
    _write_from_another_process(data_dir, _upsert("/v/c.md", 2, "gamma"))
    top = reader.query(_vec(2), k=1)[0]
    assert (top.path, top.text, top.score) == ("/v/c.md", "gamma", 1.0)
    assert reader.list_paths() == ["/v/a.md", "/v/b.md", "/v/c.md"]

    # An edited chunk is ranked by its new vector, not only shown with its new text.
    _write_from_another_process(data_dir, _upsert("/v/b.md", 3, "beta, edited"))
    top = reader.query(_vec(3), k=1)[0]
    assert (top.path, top.text, top.score) == ("/v/b.md", "beta, edited", 1.0)

    # After a prune, a search whose old top-k held the deleted chunk is still a ranked list.
    _write_from_another_process(data_dir, {"op": "delete", "path": "/v/a.md"})
    hits = reader.query(_vec(0), k=3)
    assert sorted(h.path for h in hits) == ["/v/b.md", "/v/c.md"]
    assert reader.count() == 2
    assert reader.file_hash("/v/a.md") is None


def test_reads_without_external_writes_do_not_reopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The freshness check is one small file read; the index is reloaded only after an external write."""
    data_dir = tmp_path / "data"
    store = ChromaStore(data_dir, COLLECTION, "unit-8")
    store.upsert(["x"], [_vec(0)], ["own write"], [{"path": "/v/x.md", "chunk_index": 0}])
    opens: list[int] = []
    real_open = store._open
    monkeypatch.setattr(store, "_open", lambda: (opens.append(1), real_open())[1])

    for _ in range(5):  # own writes and plain reads never reload
        store.query(_vec(0), k=1)
        store.list_paths()
        store.file_hash("/v/x.md")
        store.count()
    store.upsert(["y"], [_vec(1)], ["own write 2"], [{"path": "/v/y.md", "chunk_index": 0}])
    store.query(_vec(1), k=1)
    assert opens == []

    _write_from_another_process(data_dir, _upsert("/v/z.md", 2, "external"))
    assert store.query(_vec(2), k=1)[0].path == "/v/z.md"
    store.query(_vec(2), k=1)
    assert opens == [1]  # one reload for one external write


def test_query_skips_vectors_whose_chunk_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A vector with no metadata (its chunk deleted) yields no hit: no result without a path (constraint 2)."""
    store = ChromaStore(tmp_path / "data", COLLECTION, "unit-8")
    store.upsert(["a", "b"], [_vec(0), _vec(1)], ["A", "B"], [{"path": "/v/a.md", "chunk_index": 0}] * 2)
    stale = {"documents": [[None, "B"]], "metadatas": [[None, {"path": "/v/b.md"}]], "distances": [[0.0, 0.5]]}
    monkeypatch.setattr(store._col, "query", lambda **_: stale)
    hits = store.query(_vec(0), k=2)
    assert [(h.path, h.text) for h in hits] == [("/v/b.md", "B")]


def test_embedder_mismatch_found_on_reopen_is_still_refused(tmp_path: Path) -> None:
    """A reader that reopens onto a collection another process rebuilt under a different model
    refuses it, exactly as a fresh open would (constraint 4: never mix models)."""
    data_dir = tmp_path / "data"
    _write_from_another_process(data_dir, _upsert("/v/a.md", 0, "alpha"))
    reader = ChromaStore(data_dir, COLLECTION, "unit-8")
    assert reader.count() == 1

    # The user deletes the index and re-indexes under another embedder while `locus serve` runs.
    rebuild = f"""
import chromadb
chromadb.PersistentClient(path={str(data_dir / "chroma")!r}).delete_collection({COLLECTION!r})
"""
    env = {**os.environ, "ANONYMIZED_TELEMETRY": "False"}
    proc = subprocess.run([sys.executable, "-c", rebuild], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    _write_from_another_process(data_dir, _upsert("/v/a.md", 0, "alpha"), embedder="other-8")

    with pytest.raises(RuntimeError, match="Never mix models"):
        reader.query(_vec(0), k=1)
