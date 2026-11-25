"""Parallel searches share one freshness step: the first spends the budget, the others reuse its note instead of each
spending a full budget in turn (11 searches x 2 s was a 22 s stall with 500 files pending). Offline, fake clock."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest import freshness  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.freshness import BUSY_NOTE, _flight_of, freshen_for_serve  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

N = 30


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _SlowEmbedder(HashEmbedder):
    """Each embedded file costs one second on the fake clock."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self.clock = clock

    def embed_documents(self, texts):
        self.clock.now += 1.0
        return super().embed_documents(texts)


@pytest.fixture()
def world(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data", collection="flight", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    clock = _Clock()
    embedder = _SlowEmbedder(clock)
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(N):
        (root / f"f{i}.md").write_text(f"note {i}\n", encoding="utf-8")
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    for p in root.glob("*.md"):  # every file changes: a backlog far bigger than one budget
        p.write_text(p.read_text(encoding="utf-8") + "revised\n", encoding="utf-8")
    runs: list[int] = []
    real = freshness.freshen

    def counting(*a, **k):
        runs.append(1)
        return real(*a, **k)

    freshness.freshen = counting
    yield settings, embedder, store, clock, runs
    freshness.freshen = real


def _serve(world):
    settings, embedder, store, clock, _ = world
    return freshen_for_serve(embedder, store, settings, env={"LOCUS_FRESHNESS_BUDGET_S": "2"}, clock=clock)


def test_eleven_parallel_searches_run_one_freshen_and_share_its_note(world):
    start = world[3].now  # the setup indexing already moved the fake clock
    notes = [_serve(world) for _ in range(11)]  # what the event loop does: one after the other, no time between
    assert len(world[4]) == 1
    assert notes[0] is not None and "not yet indexed" in notes[0]
    assert all(n == notes[0] for n in notes)
    assert world[3].now - start < 5.0  # about one budget of fake time in all, not eleven


def test_the_next_search_after_the_window_freshens_again(world):
    first = _serve(world)
    assert first is not None and len(world[4]) == 1
    world[3].now += 3.0  # more than the 2 s window
    assert _serve(world) is not None and len(world[4]) == 2


def test_a_search_on_another_thread_does_not_wait_for_a_running_freshen(world):
    flight = _flight_of(world[0])
    flight.lock.acquire()  # a freshen is running
    try:
        assert _serve(world) == BUSY_NOTE and world[4] == []
    finally:
        flight.lock.release()


def test_a_run_that_finished_its_work_does_not_block_the_next_one(world):
    settings, embedder, store, clock, runs = world
    clock.now = 0.0
    big = freshen_for_serve(embedder, store, settings, env={"LOCUS_FRESHNESS_BUDGET_S": "1000"}, clock=clock)
    assert big is None and len(runs) == 1  # the backlog fit: nothing cut short
    assert freshen_for_serve(embedder, store, settings, env={"LOCUS_FRESHNESS_BUDGET_S": "1000"}, clock=clock) is None
    assert len(runs) == 2  # not coalesced: the first run was not cut short
