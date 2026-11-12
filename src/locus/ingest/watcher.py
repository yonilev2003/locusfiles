"""Keep the index in sync with a folder while it changes (watchdog, per-path debounce).

RET-008: when the embedder refuses for a reason that affects every file (budget used up, key or
subscription refused, operator pause, or a rate limit or outage that outlasted a file's retries and
also failed a one-line test request, retry.probe_service), the watcher pauses and queues the
changed files instead of failing each one. It logs the pause once and retries the queue when the
budget resets (or after the wait the server asked for, or a default pause). When the test request
goes through, the failure is that file's own: it is logged, the watcher keeps indexing other
changes with no pause, and the file is retried once after the default pause in case its failure
was brief (re-review NEW-4); if that retry fails too, it waits for its next change.

Stopping (Ctrl-C) does not wait out a retry backoff that a worker is in: those waits end as soon
as the watcher stops (review F5), and queued work that has not started is cancelled.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from ..config import SKIP_DIRS, Settings
from .embedder import Embedder
from .errors import BudgetExhausted, EmbedError, as_embed_error
from .indexer import file_sha256, index_file, remove_path
from .retry import DEFAULT_RETRY, RetryPolicy, probe_service
from .store import VectorStore


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


class WatchStopping(Exception):  # noqa: N818 - a signal to unwind a worker, not an error
    """Raised inside a worker's retry wait when the watcher is stopping; the file is left as it was."""


def interruptible(policy: RetryPolicy, stopping: threading.Event) -> RetryPolicy:
    """*policy* whose backoff waits end as soon as *stopping* is set, by raising WatchStopping.
    A retry wait can last up to `max_delay` (300 s) and a batch can wait several times, so without
    this Ctrl-C in `locus watch` blocked until every worker had slept its backoff out."""
    wait_out = policy.sleep

    def _sleep(seconds: float) -> None:
        if wait_out is time.sleep:
            stopped = stopping.wait(seconds)
        else:  # a caller-supplied sleep (tests): keep it, then check
            wait_out(seconds)
            stopped = stopping.is_set()
        if stopped:
            raise WatchStopping

    return dataclasses.replace(policy, sleep=_sleep)


class PendingQueue:
    """Changed files the watcher could not index because of a run-wide refusal, kept until the pause
    ends; and files skipped for their own failure, each retried once after the default pause.
    Thread-safe: the watcher's worker pool and its main loop share one instance."""

    def __init__(
        self,
        default_pause: float = 900.0,  # no reset time known (key, subscription, outage): retry in 15 min
        max_pause: float = 24 * 3600.0,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._lock = threading.Lock()
        self._paths: set[str] = set()
        self._resume_at: float | None = None
        self._default_pause = default_pause
        self._max_pause = max_pause
        self._clock = clock
        self._now = now
        # Files skipped for their own failure: path -> (when to retry, sha256 of what failed), and
        # path -> sha256 of the content already given its one retry.
        self._later: dict[str, tuple[float, str]] = {}
        self._retried: dict[str, str] = {}

    @property
    def default_pause(self) -> float:
        return self._default_pause

    def pause_seconds(self, err: EmbedError) -> float:
        if isinstance(err, BudgetExhausted) and err.resets_at is not None:
            seconds = (err.resets_at - self._now()).total_seconds()
        elif err.retry_after is not None:
            seconds = err.retry_after
        else:
            seconds = self._default_pause
        return min(max(seconds, 0.0), self._max_pause)

    def hold(self, paths: Iterable[str], err: EmbedError) -> bool:
        """Queue *paths* and (re)start the pause for *err*. True when this starts a new pause, so
        the caller logs it once rather than once per file."""
        with self._lock:
            self._paths.update(paths)
            started = self._resume_at is None
            self._resume_at = self._clock() + self.pause_seconds(err)
            return started

    def defer(self, path: str) -> bool:
        """While a pause is on, queue *path* (no embedding call is made) and return True."""
        with self._lock:
            if self._resume_at is None:
                return False
            self._paths.add(path)
            return True

    def due(self) -> list[str]:
        """Once the pause is over: end it and hand back the queued paths. Otherwise []."""
        with self._lock:
            if self._resume_at is None or self._clock() < self._resume_at:
                return []
            paths = sorted(self._paths)
            self._paths.clear()
            self._resume_at = None
            return paths

    def retry_later(self, path: str, digest: str) -> bool:
        """*path* (content *digest*) failed on its own: the service answered a test request. Retry
        it once after the default pause, in case the failure was brief (a short outage or rate limit
        that hit only this file). False when this content already had that retry: it then waits for
        its next change, so a file that always fails is not re-sent every cycle."""
        with self._lock:
            if self._retried.get(path) == digest:
                return False
            self._later[path] = (self._clock() + self._default_pause, digest)
            return True

    def retries_due(self) -> list[str]:
        """Skipped files whose one retry is due (none while a pause is on)."""
        with self._lock:
            now = self._clock()
            if self._resume_at is not None:
                return []
            due = sorted(path for path, (at, _) in self._later.items() if at <= now)
            for path in due:
                self._retried[path] = self._later.pop(path)[1]
            return due

    def settled(self, path: str) -> None:
        """*path* is indexed as it is now: drop any retry it was waiting for."""
        with self._lock:
            self._later.pop(path, None)
            self._retried.pop(path, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._paths)


def _about(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min" if minutes < 90 else f"{minutes / 60:.1f} h"


def pause_message(err: EmbedError, queued: int, seconds: float) -> str:
    when = _about(seconds)
    parts = [f"Paused indexing: {str(err).rstrip('.')}."]
    upgrade_url = getattr(err, "upgrade_url", None)
    if upgrade_url and upgrade_url not in str(err):
        parts.append(f"Plan and billing: {upgrade_url}")
    parts.append(
        f"{queued} changed file(s) are queued, not dropped; the watcher retries them in about {when} "
        "and keeps queuing new changes until then. Chunks already indexed are kept."
    )
    return " ".join(parts)


def watch_folder(
    root: Path,
    embedder: Embedder,
    store: VectorStore,
    settings: Settings,
    debounce_seconds: float = 1.5,
    retry: RetryPolicy = DEFAULT_RETRY,
    pending: PendingQueue | None = None,
    poll_seconds: float = 1.0,
    stop: threading.Event | None = None,
) -> None:
    """Watch *root* until Ctrl+C (or until `stop` is set). `pending` may arrive already holding
    files (e.g. those an initial `index_folder` pass stopped before reaching); they are retried
    when its pause ends."""
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    pending = pending if pending is not None else PendingQueue()
    stopping = stop if stop is not None else threading.Event()
    retry = interruptible(retry, stopping)

    root = Path(root).expanduser().resolve()
    timers: dict[str, threading.Timer] = {}
    lock = threading.Lock()
    # Each debounced path used to spawn its own thread.Timer thread, so saving many files at
    # once (a bulk edit, a git checkout, several people editing at the same time) launched
    # unbounded concurrent index_file calls — parallel embedding-API requests and concurrent
    # writes to the same store. A small bounded pool serializes the actual work; the debounce
    # timers themselves stay one-per-path (that part was correct) but just submit to it.
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="locus-watch")

    def _path_eligible(path: Path) -> bool:
        """Extension/skip-dir check only — safe to call on a path that no longer exists,
        since it does no filesystem access. Used to decide whether a *removal* applies."""
        if path.suffix.lower() not in settings.extensions:
            return False
        return not any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(root).parts)

    def wanted(path: Path) -> bool:
        """Full eligibility for (re)indexing an existing file — extension, skip-dirs, and size."""
        if not _path_eligible(path):
            return False
        if path.is_symlink():
            return False  # like `locus index`: a link can point anywhere, so it is never followed
        try:
            # `locus index` skips oversized files (indexer.iter_files); the watcher must
            # apply the same LOCUS_MAX_FILE_MB cap, not just the extension filter, or a
            # large file added while watching gets indexed when a full `locus index` run
            # of the same folder would have skipped it.
            return path.stat().st_size <= int(settings.max_file_mb * 1024 * 1024)
        except OSError:
            return False

    def skip_own_failure(path: Path, exc: Exception) -> None:
        """A one-line test request went through, so only *path*'s request fails. Log it and keep
        indexing other changes, instead of pausing everything for it (and again every time the
        pause ends: re-review NEW-1); retry it once later in case the failure was brief (NEW-4)."""
        try:
            again = pending.retry_later(str(path), file_sha256(path))
        except OSError:  # gone or unreadable since: its own filesystem event handles it
            again = False
        if again:
            when = _about(pending.default_pause)
            _log(f"ERROR {path}: {exc} — skipped; retrying it once in about {when} (or on its next change)")
        else:
            _log(f"ERROR {path}: {exc} — skipped until its next change; the watcher goes on")

    def schedule(path: Path) -> None:
        def run() -> None:
            with lock:
                timers.pop(str(path), None)
            if stopping.is_set():
                return
            if not path.exists() or path.is_symlink():  # gone, or replaced by a link (never indexed)
                remove_path(path, store, log=_log)
                return
            if pending.defer(str(path)):
                return  # paused: queue it, send nothing
            try:
                index_file(path, root, embedder, store, settings, log=_log, retry=retry)
            except WatchStopping:
                return  # stopping mid-backoff: the file keeps its old chunks
            except Exception as exc:  # noqa: BLE001
                typed = as_embed_error(exc)
                if typed is None or not typed.stops_run:
                    _log(f"ERROR {path}: {exc}")
                    return
                if stopping.is_set():
                    return  # no test request while stopping; the file keeps its old chunks
                refusal = probe_service(embedder, typed, retry) if retry.could_be_request_specific(typed) else typed
                if refusal is None:
                    skip_own_failure(path, exc)
                elif pending.hold([str(path)], refusal):
                    _log(pause_message(refusal, len(pending), pending.pause_seconds(refusal)))
            else:
                pending.settled(str(path))

        def submit() -> None:
            if stopping.is_set():
                return
            try:
                executor.submit(run)
            except RuntimeError:  # the pool shut down between the check and the submit
                pass

        with lock:
            if (t := timers.get(str(path))) is not None:
                t.cancel()
            timer = threading.Timer(debounce_seconds, submit)
            timers[str(path)] = timer
            timer.daemon = True
            timer.start()

    class Handler(FileSystemEventHandler):
        def on_created(self, event: FileSystemEvent) -> None:
            self._touch(event)

        def on_modified(self, event: FileSystemEvent) -> None:
            self._touch(event)

        def on_deleted(self, event: FileSystemEvent) -> None:
            if event.is_directory:
                return
            path = Path(str(event.src_path))
            if _path_eligible(path):
                remove_path(path, store, log=_log)

        def on_moved(self, event: FileSystemEvent) -> None:
            if event.is_directory:
                return
            src = Path(str(event.src_path))
            if _path_eligible(src):
                remove_path(src, store, log=_log)
            dest = Path(str(event.dest_path))
            if wanted(dest):
                schedule(dest)

        def _touch(self, event: FileSystemEvent) -> None:
            if event.is_directory:
                return
            path = Path(str(event.src_path))
            if wanted(path):
                schedule(path)
            elif _path_eligible(path) and path.is_symlink():
                remove_path(path, store, log=_log)  # a file replaced by a link loses its old chunks

    observer = Observer()
    observer.schedule(Handler(), str(root), recursive=True)
    observer.start()
    _log(f"watching {root} (Ctrl+C to stop)")
    try:
        while not stopping.wait(poll_seconds):
            queued = pending.due()
            if queued:
                _log(f"resuming: retrying {len(queued)} queued file(s)")
                for queued_path in queued:
                    schedule(Path(queued_path))
            retries = pending.retries_due()
            if retries:
                _log(f"retrying {len(retries)} file(s) skipped earlier for their own failure (once)")
                for retry_path in retries:
                    schedule(Path(retry_path))
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()  # ends any retry wait in a worker (interruptible) and skips queued runs
        observer.stop()
        observer.join()
        for t in list(timers.values()):
            t.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
