"""Folder-scoped search must scale (W5): `path_prefix` used to ask Chroma for every chunk, which
failed with `too many SQL variables` from about 32,700 chunks. These tests build a store past that
size with synthetic chunks (offline, no embedder) and check that scoped reads work and that results
match a brute-force reference."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.ingest.store import ChromaStore, _under  # noqa: E402

DIM = 8
N_CHUNKS = 33_500
FOLDERS = 25


def _fill(store: ChromaStore, n: int, seed: int = 7) -> dict[str, list[float]]:
    """n chunks over FOLDERS folders (two levels deep); returns id -> vector."""
    rng = random.Random(seed)
    vecs: dict[str, list[float]] = {}
    batch: tuple[list, list, list, list] = ([], [], [], [])
    for i in range(n):
        folder = i % FOLDERS
        path = f"/data/root{folder % 5}/sub{folder}/file{i // 4}.txt"
        vec = [rng.random() for _ in range(DIM)]
        cid = f"{i}"
        vecs[cid] = vec
        batch[0].append(cid)
        batch[1].append(vec)
        batch[2].append(f"chunk {i}")
        batch[3].append({"path": path, "chunk_index": i % 4, "mtime": 1000.0 + i})
        if len(batch[0]) == 5000:
            store.upsert(*batch)
            batch = ([], [], [], [])
    if batch[0]:
        store.upsert(*batch)
    return vecs


@pytest.fixture(scope="module")
def big_store(tmp_path_factory) -> tuple[ChromaStore, dict[str, list[float]]]:
    store = ChromaStore(tmp_path_factory.mktemp("big"), "scale", "hash")
    vecs = _fill(store, N_CHUNKS)
    return store, vecs


def test_scoped_query_past_sql_variable_limit(big_store) -> None:
    store, _ = big_store
    assert store.count() > 33_000
    hits = store.query([0.5] * DIM, k=5, path_prefix="/data/root1/sub1")
    assert len(hits) == 5
    assert all(_under(h.path, "/data/root1/sub1") for h in hits)
    assert all(h.path and h.chunk_index in range(4) for h in hits)


def test_scoped_query_matches_brute_force(big_store) -> None:
    store, vecs = big_store
    q = [0.3, 0.9, 0.1, 0.5, 0.7, 0.2, 0.8, 0.4]
    prefix = "/data/root2"
    scoped = store.query(q, k=8, path_prefix=prefix)
    got = store._col.get(include=["metadatas"], limit=5000, offset=0)  # noqa: SLF001 - ids to paths, paged
    paths: dict[str, tuple[str, int]] = {}
    for off in range(0, N_CHUNKS, 5000):
        res = store._col.get(include=["metadatas"], limit=5000, offset=off)  # noqa: SLF001
        for cid, m in zip(res["ids"], res["metadatas"], strict=True):
            paths[cid] = (m["path"], m["chunk_index"])
    assert got["ids"]

    def dist(v: list[float]) -> float:  # cosine distance, like the collection
        dot = sum(a * b for a, b in zip(q, v, strict=True))
        return 1 - dot / (sum(a * a for a in q) ** 0.5 * sum(b * b for b in v) ** 0.5)

    ranked = sorted((cid for cid in vecs if _under(paths[cid][0], prefix)), key=lambda c: dist(vecs[c]))[:8]
    assert [(h.path, h.chunk_index) for h in scoped] == [paths[c] for c in ranked]


def test_scoped_time_window_and_files_modified(big_store) -> None:
    store, _ = big_store
    hits = store.query([0.5] * DIM, k=3, path_prefix="/data/root3", modified_after=2000.0, modified_before=3000.0)
    assert hits and all(2000.0 <= (h.mtime or 0) < 3000.0 and _under(h.path, "/data/root3") for h in hits)
    files = store.files_modified(k=5, path_prefix="/data/root3")
    assert len(files) == 5 and all(_under(f.path, "/data/root3") for f in files)
    assert store.count_files_modified(path_prefix="/data/root3") > 5


# -- small stores: the keyed path, the legacy fallback and the old behaviour agree ---------------


def _legacy_collection(data_dir: Path, n: int) -> None:
    """A collection as built before W5: no `dir_keys` flag and no dk* keys on its chunks."""
    import chromadb

    col = chromadb.PersistentClient(path=str(data_dir / "chroma")).get_or_create_collection(
        name="small", metadata={"hnsw:space": "cosine", "embedder": "hash"}
    )
    rng = random.Random(3)
    for lo in range(0, n, 5000):
        ids = [str(i) for i in range(lo, min(n, lo + 5000))]
        col.upsert(
            ids=ids,
            embeddings=[[rng.random() for _ in range(DIM)] for _ in ids],
            documents=[f"chunk {i}" for i in ids],
            metadatas=[
                {
                    "path": f"/data/root{int(i) % 3}/sub{int(i) % 7}/f{int(i) // 5}.txt",
                    "chunk_index": int(i) % 5,
                    "mtime": 10.0 + int(i),
                }
                for i in ids
            ],
        )


PREFIXES = [
    "/data/root1",
    "/data/root1/",
    "/data/root1/sub1",
    "/data/root2/sub2/f4.txt",  # one file named as the prefix
    "/data/root",
    "/data/root1/sub99",
    "/data",
]


def _reference(store: ChromaStore, q: list[float], k: int, prefix: str | None) -> list[tuple[str, int]]:
    """The old behaviour: rank every chunk, then keep those under the prefix."""
    hits = store.query(q, k=store.count(), path_prefix=None)
    return [(h.path, h.chunk_index) for h in hits if prefix is None or _under(h.path, prefix)][:k]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_keyed_and_legacy_agree_with_old_behaviour(tmp_path: Path, prefix: str) -> None:
    _legacy_collection(tmp_path, 600)
    q = [0.2, 0.4, 0.6, 0.8, 0.1, 0.3, 0.5, 0.7]
    store = ChromaStore(tmp_path, "small", "hash")
    assert not store._has_dir_keys  # noqa: SLF001
    expected = _reference(store, q, 7, prefix)
    legacy = [(h.path, h.chunk_index) for h in store.query(q, k=7, path_prefix=prefix)]
    assert legacy == expected
    assert store.ensure_dir_keys() == 600
    assert store._has_dir_keys and store.ensure_dir_keys() == 0  # noqa: SLF001
    assert [(h.path, h.chunk_index) for h in store.query(q, k=7, path_prefix=prefix)] == expected
    reopened = ChromaStore(tmp_path, "small", "hash")
    assert reopened._has_dir_keys  # noqa: SLF001
    assert [(h.path, h.chunk_index) for h in reopened.query(q, k=7, path_prefix=prefix)] == expected


def test_new_collection_scope_edge_cases(tmp_path: Path) -> None:
    store = ChromaStore(tmp_path, "edge", "hash")
    deep = "/" + "/".join(f"d{i}" for i in range(20))  # deeper than the 16 stored levels
    nfd = "/data/café/a.txt"
    rows = [
        (f"{deep}/x/a.txt", 0),
        (f"{deep}2/b.txt", 0),  # sibling folder sharing the string prefix
        (nfd, 0),
        ("/data/notes/a.txt", 0),
        ("/data/notes2/a.txt", 0),
    ]
    store.upsert(
        [str(i) for i in range(len(rows))],
        [[0.1 * (i + 1)] * DIM for i in range(len(rows))],
        ["t"] * len(rows),
        [{"path": p, "chunk_index": c, "mtime": 1.0} for p, c in rows],
    )
    q = [0.5] * DIM
    assert {h.path for h in store.query(q, k=10, path_prefix=deep)} == {f"{deep}/x/a.txt"}
    assert {h.path for h in store.query(q, k=10, path_prefix=f"{deep}/x/a.txt")} == {f"{deep}/x/a.txt"}
    assert {h.path for h in store.query(q, k=10, path_prefix="/data/notes")} == {"/data/notes/a.txt"}
    assert {h.path for h in store.query(q, k=10, path_prefix="/data/café")} == {nfd}  # typed composed
    assert store.query(q, k=10, path_prefix="/nowhere") == []


def test_big_listing_helpers_page(big_store) -> None:
    store, _ = big_store
    assert len(store.file_mtimes()) == len(store.list_paths()) > 8000
    assert store.indexed_roots() == []  # these chunks carry no root; the point is that it no longer raises
