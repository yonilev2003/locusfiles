"""One count of changed files for locus_diagnose_connection and the freshness note that locus_list_sources and
locus_search carry: the same number, from the same function. Offline, HashEmbedder."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.freshness import count_pending, freshen_for_serve  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

N = 40


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data", collection="pend", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(N):
        (root / f"f{i}.md").write_text(f"note {i}: decision about topic {i}\n", encoding="utf-8")
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    srv._state.update(embedder=embedder, store=store, settings=settings)
    yield root, settings, embedder, store
    srv._state.clear()


def _touch_all(root: Path) -> None:
    later = time.time() + 120
    for p in root.glob("*.md"):
        os.utime(p, (later, later))


def test_count_pending_counts_changed_gone_and_says_how_far_it_checked(world):
    root, _, _, store = world
    assert count_pending(store).changed == 0  # type: ignore[union-attr]
    _touch_all(root)
    (root / "f0.md").unlink()
    counts = count_pending(store)
    assert counts is not None
    assert (counts.changed, counts.gone, counts.checked, counts.total) == (N - 1, 1, N, N)
    assert counts.scope.startswith(f"checked {N} of {N} indexed files in ")


def test_count_pending_stops_at_its_time_budget_and_says_so(world):
    root, _, _, store = world
    _touch_all(root)
    ticks = iter(range(1000))
    counts = count_pending(store, budget_s=3.5, clock=lambda: float(next(ticks)))
    assert counts is not None and not counts.complete
    assert counts.checked < counts.total and counts.scope.startswith(f"checked {counts.checked} of {N}")


def test_diagnose_and_the_freshness_note_report_the_same_number(world):
    root, settings, embedder, store = world
    _touch_all(root)  # 40 files changed on disk since they were indexed
    state = "\n".join(srv._index_state_lines(store))
    assert re.search(rf"stale \({N} changed on disk and 0 gone", state)

    # the budget ends before the scan finds anything: the note carries the same count
    note = freshen_for_serve(embedder, store, settings, env={"LOCUS_FRESHNESS_BUDGET_S": "0.000001"})
    assert note is not None and f"{N} indexed file(s) changed on disk and 0 gone" in note
    assert f"checked {N} of {N} indexed files" in note


class _SlowEmbedder(HashEmbedder):
    """Each embedded file costs one second on the fake clock."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def embed_documents(self, texts):
        self.clock.now += 1.0
        return super().embed_documents(texts)


def test_pending_after_a_partial_index_matches_the_diagnostic(world):
    from .test_freshness import FakeClock

    root, settings, _, store = world
    for p in root.glob("*.md"):  # new content, or the indexer would only refresh the recorded time
        p.write_text(p.read_text(encoding="utf-8") + "revised\n", encoding="utf-8")
    _touch_all(root)
    clock = FakeClock()
    note = freshen_for_serve(_SlowEmbedder(clock), store, settings, env={"LOCUS_FRESHNESS_BUDGET_S": "5"}, clock=clock)
    assert note is not None
    left = count_pending(store).changed  # type: ignore[union-attr]
    assert 0 < left < N
    assert f"{left} changed file(s) not yet indexed" in note
    assert f"{left} indexed file(s) changed on disk" in note
    assert f"stale ({left} changed on disk" in "\n".join(srv._index_state_lines(store))


def test_list_sources_carries_the_same_count(world, monkeypatch):
    root, _, _, _ = world
    _touch_all(root)
    monkeypatch.setenv("LOCUS_FRESHNESS_BUDGET_S", "0.000001")
    out = asyncio.run(srv.locus_list_sources(limit=5))
    assert f"{N} indexed file(s) changed on disk and 0 gone" in out
    diag = "\n".join(srv._index_state_lines(world[3]))
    assert f"{N} changed on disk" in diag
