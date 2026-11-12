"""Integration tests for the file watcher — real watchdog Observer, generous timeouts."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.embedder import HashEmbedder
from locus.ingest.errors import AuthFailed, BudgetExhausted, ProviderUnavailable, RateLimited
from locus.ingest.retry import RetryPolicy

pytest.importorskip("watchdog")
chromadb = pytest.importorskip("chromadb")

from locus.ingest.store import ChromaStore  # noqa: E402
from locus.ingest.watcher import PendingQueue, watch_folder  # noqa: E402


def _settings(tmp_path: Path, max_file_mb: float = 5) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=100,
        overlap_tokens=10,
        max_file_mb=max_file_mb,
        extensions=DEFAULT_EXTENSIONS,
    )


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_watch_respects_max_file_mb(tmp_path: Path):
    """Regression: the watcher used to index files over LOCUS_MAX_FILE_MB that a plain
    `locus index` run of the same folder would have skipped — only the extension was checked."""
    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path, max_file_mb=0.000_01)  # ~10 bytes cap
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)

    # watch_folder only returns on KeyboardInterrupt; run it in a daemon thread and just let
    # the thread be abandoned when the test process exits — there is no other stop hook.
    t = threading.Thread(target=lambda: _run_ignoring_errors(root, embedder, store, settings), daemon=True)
    t.start()
    time.sleep(0.3)  # let the observer start
    (root / "big.md").write_text("This file is comfortably over a ten-byte cap.")
    time.sleep(1.5)  # generous margin past the 0.2s debounce for a background thread-pool run
    assert store.count() == 0, "an oversized file was indexed by the watcher"


def test_watch_removes_deleted_file_from_index(tmp_path: Path):
    """Regression: wanted() called path.stat() to check the size cap, even when deciding
    whether to *remove* an already-deleted path — stat() raises on a gone file, wanted()
    swallowed that into False, so on_deleted/on_moved's source-removal branch never fired
    remove_path for a genuine delete."""
    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path)
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)

    target = root / "gone.md"
    target.write_text("This file will be deleted while being watched.")

    t = threading.Thread(target=lambda: _run_ignoring_errors(root, embedder, store, settings), daemon=True)
    t.start()
    time.sleep(0.3)  # let the observer start
    target.write_text("indexed first, then deleted")  # trigger a create/modify to index it
    assert _wait_until(lambda: store.count() > 0), "file was never indexed before deletion"

    target.unlink()
    assert _wait_until(lambda: store.count() == 0), "deleted file's chunks were never removed from the index"


def _run_ignoring_errors(root, embedder, store, settings) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        watch_folder(root, embedder, store, settings, debounce_seconds=0.2)


# --- RET-008: pause and queue instead of failing per file ---------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_pending_queue_holds_until_the_budget_resets():
    clock = _Clock()
    now = datetime(2026, 9, 23, 23, 0, tzinfo=UTC)
    queue = PendingQueue(clock=clock, now=lambda: now)
    err = BudgetExhausted("daily budget used up", scope="daily", resets_at=now + timedelta(hours=1))

    assert queue.defer("/x/a.md") is False  # not paused: index normally
    assert queue.hold(["/x/a.md"], err) is True  # first refusal starts the pause (log once)
    assert queue.hold(["/x/b.md"], err) is False  # a concurrent refusal does not log again
    assert queue.defer("/x/c.md") is True  # while paused, new changes queue without an embed call
    assert len(queue) == 3
    clock.t += 3599
    assert queue.due() == []
    clock.t += 2
    assert queue.due() == ["/x/a.md", "/x/b.md", "/x/c.md"]
    assert queue.due() == [] and queue.defer("/x/d.md") is False  # the pause is over


def test_pending_queue_pause_lengths():
    queue = PendingQueue(default_pause=900, max_pause=3600)
    assert queue.pause_seconds(RateLimited("x", retry_after=40)) == 40
    assert queue.pause_seconds(AuthFailed("bad key")) == 900
    far = BudgetExhausted("x", resets_at=datetime.now(UTC) + timedelta(days=30))
    assert queue.pause_seconds(far) == 3600  # capped, so a long reset is re-checked
    trial = BudgetExhausted("x", scope="trial")  # never resets: re-check after the default pause
    assert queue.pause_seconds(trial) == 900


def test_pending_queue_retries_a_skipped_file_once_per_content():
    """Re-review NEW-4: a file skipped for its own failure is retried once after the default pause
    (the failure may have been brief), not left stale until its next edit — and not retried again
    and again either: the same content gets one retry."""
    clock = _Clock()
    queue = PendingQueue(default_pause=900, clock=clock)
    assert queue.retry_later("/x/a.md", "sha-1") is True
    assert queue.retries_due() == []
    clock.t += 900
    assert queue.retries_due() == ["/x/a.md"]
    assert queue.retries_due() == []
    assert queue.retry_later("/x/a.md", "sha-1") is False  # failed again after its retry: wait for an edit
    assert queue.retry_later("/x/a.md", "sha-2") is True  # edited since: the new content gets its retry
    queue.settled("/x/a.md")  # ...and was indexed meanwhile: nothing left to retry
    clock.t += 900
    assert queue.retries_due() == []
    assert len(queue) == 0  # retries are not the paused queue

    queue.retry_later("/x/b.md", "sha-b")
    queue.hold([], RateLimited("x", retry_after=2000))
    clock.t += 1000
    assert queue.retries_due() == []  # a pause is on: the retry waits for it
    clock.t += 1000
    assert queue.due() == [] and queue.retries_due() == ["/x/b.md"]


class _RefusingEmbedder(HashEmbedder):
    """Refuses the first call with a budget stop that resets `pause` seconds later, then works."""

    def __init__(self, pause: float) -> None:
        super().__init__()
        self.calls = 0
        self.pause = pause

    def embed_documents(self, texts):
        self.calls += 1
        if self.calls == 1:
            raise BudgetExhausted(
                "Locus embed relay limit reached (HTTP 429): Daily embedding quota exceeded",
                scope="daily",
                resets_at=datetime.now(UTC) + timedelta(seconds=self.pause),
            )
        return super().embed_documents(texts)


def test_watch_queues_changes_during_a_budget_pause_and_retries_after(tmp_path: Path, capsys):
    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path)
    embedder = _RefusingEmbedder(pause=1.0)
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)

    def _run() -> None:
        import contextlib

        with contextlib.suppress(Exception):
            no_wait = RetryPolicy(sleep=lambda _s: None)
            watch_folder(root, embedder, store, settings, debounce_seconds=0.1, retry=no_wait, poll_seconds=0.1)

    threading.Thread(target=_run, daemon=True).start()
    time.sleep(0.3)
    (root / "first.md").write_text("Written while the budget is used up.")
    assert _wait_until(lambda: embedder.calls == 1), "the first change was never attempted"
    (root / "second.md").write_text("Also written during the pause.")
    time.sleep(0.4)
    assert embedder.calls == 1, "a change during the pause reached the embedder"
    assert store.count() == 0
    assert _wait_until(lambda: len(store.list_paths()) == 2, timeout=6), "queued files were not retried"
    err = capsys.readouterr().err
    assert err.count("Paused indexing") == 1


class _OutageEmbedder(HashEmbedder):
    """Every request whose texts match `bad` gets a 502-style ProviderUnavailable (all of them when
    `bad` is None); the rest embed normally."""

    def __init__(self, bad: str | None = None) -> None:
        super().__init__()
        self.bad = bad
        self.calls = 0
        self._lock = threading.Lock()

    def embed_documents(self, texts):
        with self._lock:
            self.calls += 1
        if self.bad is None or any(self.bad in t for t in texts):
            raise ProviderUnavailable("Locus embed relay returned HTTP 502: Embedding provider error.")
        return super().embed_documents(texts)


@pytest.fixture()
def watch_stop():
    """Stops the test's background watcher at teardown, so it cannot keep logging into (and
    embedding during) later tests."""
    stop = threading.Event()
    yield stop
    stop.set()


def _watch_in_background(root, embedder, store, settings, pending=None, stop=None) -> None:
    def _run() -> None:
        import contextlib

        with contextlib.suppress(Exception):
            no_wait = RetryPolicy(sleep=lambda _s: None)
            watch_folder(
                root,
                embedder,
                store,
                settings,
                debounce_seconds=0.1,
                retry=no_wait,
                pending=pending,
                poll_seconds=0.1,
                stop=stop,
            )

    threading.Thread(target=_run, daemon=True).start()
    time.sleep(0.3)  # let the observer start


def test_watch_skips_one_failing_file_without_pausing_the_others(tmp_path: Path, capsys, watch_stop):
    """RET-008 review F1: one file whose request always fails used to start a 15-minute pause (and
    restart it on every save), holding every other change in the queue. It is now that file's
    error only; other changes keep being indexed."""
    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path)
    embedder = _OutageEmbedder(bad="poison")
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    pending = PendingQueue()
    _watch_in_background(root, embedder, store, settings, pending=pending, stop=watch_stop)

    (root / "poison.md").write_text("This poison file is never accepted by the provider.")
    assert _wait_until(lambda: embedder.calls >= 3), "the failing file was never attempted"
    (root / "good.md").write_text("A normal note written right after.")
    assert _wait_until(lambda: any(p.endswith("good.md") for p in store.list_paths())), (
        "a change after one failing file was held back"
    )
    assert len(pending) == 0
    err = capsys.readouterr().err
    assert "Paused indexing" not in err
    assert "poison.md" in err and "retrying it once" in err


class _BrieflyFailing(HashEmbedder):
    """Requests with "flaky" in them fail (502) until `healed` is set; everything else embeds."""

    def __init__(self) -> None:
        super().__init__()
        self.healed = threading.Event()
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        if not self.healed.is_set() and any("flaky" in t for t in texts):
            raise ProviderUnavailable("Locus embed relay returned HTTP 502: Embedding provider error.")
        return super().embed_documents(texts)


def test_a_file_skipped_for_a_brief_failure_is_retried_once_later(tmp_path: Path, capsys, watch_stop):
    """Re-review NEW-4: a short outage or rate limit that hits one saved file (the service answers
    the test request) left it with its old chunks until the user happened to edit it again."""
    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path)
    embedder = _BrieflyFailing()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    pending = PendingQueue(default_pause=0.5)
    _watch_in_background(root, embedder, store, settings, pending=pending, stop=watch_stop)

    (root / "note.md").write_text("A flaky moment: this save hits a brief provider failure.")
    assert _wait_until(lambda: "retrying it once" in capsys.readouterr().err, timeout=3), "not set up for a retry"
    embedder.healed.set()
    assert _wait_until(lambda: any(p.endswith("note.md") for p in store.list_paths()), timeout=5), (
        "the skipped file was never retried"
    )


def test_a_file_that_keeps_failing_is_retried_only_once(tmp_path: Path, capsys, watch_stop):
    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path)
    embedder = _OutageEmbedder(bad="poison")
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    pending = PendingQueue(default_pause=0.3)
    _watch_in_background(root, embedder, store, settings, pending=pending, stop=watch_stop)

    (root / "poison.md").write_text("This poison file is never accepted by the provider.")
    time.sleep(2.0)  # the first attempt, its retry 0.3 s later, and several more 0.3 s periods
    # each attempt: 3 tries and one test request
    assert embedder.calls == 2 * (3 + 1)
    err = capsys.readouterr().err
    assert err.count("retrying it once") == 1 and "skipped until its next change" in err
    assert "Paused indexing" not in err


def test_several_failing_files_neither_pause_the_watcher_nor_loop(tmp_path: Path, capsys, watch_stop):
    """Re-review NEW-1 (watch path): three files that always fail used to fill the three-in-a-row
    streak, pause the watcher, get retried when the pause ended, fail again and pause again, every
    15 minutes for as long as it ran (5 pauses in 3 s with a 0.5 s pause). A one-line test request
    goes through, so they are those files' errors: no pause, and the embedder calls level off."""
    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path)
    embedder = _OutageEmbedder(bad="poison")
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    pending = PendingQueue(default_pause=0.5)
    _watch_in_background(root, embedder, store, settings, pending=pending, stop=watch_stop)

    (root / "good.md").write_text("A normal note.")
    assert _wait_until(lambda: any(p.endswith("good.md") for p in store.list_paths())), "good.md was not indexed"
    for name in ("p1.md", "p2.md", "p3.md"):
        (root / name).write_text(f"This poison note {name} is never accepted by the provider.")
        time.sleep(0.2)
    time.sleep(2.5)  # several 0.5 s pauses' worth: a pause/resume loop would keep sending
    settled = embedder.calls
    time.sleep(1.5)
    err = capsys.readouterr().err
    assert embedder.calls == settled, f"the failing files keep being re-sent:\n{err}"
    assert len(pending) == 0
    assert "Paused indexing" not in err and "resuming" not in err
    (root / "later.md").write_text("A normal note written afterwards.")
    assert _wait_until(lambda: any(p.endswith("later.md") for p in store.list_paths())), "later.md was not indexed"


def test_watch_pauses_once_when_the_service_is_down(tmp_path: Path, capsys, watch_stop):
    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path)
    embedder = _OutageEmbedder()  # the service is down: every request fails, the test request too
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    pending = PendingQueue()
    _watch_in_background(root, embedder, store, settings, pending=pending, stop=watch_stop)

    for name in ("one.md", "two.md", "three.md"):
        (root / name).write_text(f"Note {name} written during an outage.")
        time.sleep(0.2)
    assert _wait_until(lambda: len(pending) == 3), "the changes were not queued for the pause"
    err = capsys.readouterr().err
    assert err.count("Paused indexing") == 1


def test_stopping_the_watcher_does_not_wait_out_a_retry_backoff(tmp_path: Path):
    """Review F5: workers slept through retry waits (up to 300 s each, several per batch) and the
    watcher's shutdown waited for them, so Ctrl-C under a rate limit took minutes to return."""

    class _Throttled(HashEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def embed_documents(self, texts):
            self.calls += 1
            raise RateLimited("Locus embed relay is rate-limiting requests (HTTP 429): slow down", retry_after=30)

    root = tmp_path / "notes"
    root.mkdir()
    settings = _settings(tmp_path)
    embedder = _Throttled()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    stop = threading.Event()
    returned = threading.Event()

    def _run() -> None:
        try:
            # The real time.sleep: a 30 s backoff (Retry-After) is what the worker is in when we stop.
            watch_folder(root, embedder, store, settings, debounce_seconds=0.1, poll_seconds=0.1, stop=stop)
        finally:
            returned.set()

    threading.Thread(target=_run, daemon=True).start()
    time.sleep(0.3)
    (root / "note.md").write_text("Written while the relay is rate-limiting.")
    assert _wait_until(lambda: embedder.calls == 1), "the change was never attempted"
    time.sleep(0.2)  # the worker is now in its 30 s wait
    started = time.monotonic()
    stop.set()  # what Ctrl-C does via the finally block
    assert returned.wait(5), "the watcher waited out the retry backoff before stopping"
    assert time.monotonic() - started < 5
    assert embedder.calls == 1  # no retry was sent after the stop
