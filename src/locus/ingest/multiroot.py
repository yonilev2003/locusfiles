"""Index several roots under the policy matcher: staged, deduplicated, resumable.

Same walker and deny-list as `locus index --roots --dry-run` (indexer.iter_files + policy.Matcher), one
collection. A re-run skips what is already indexed, so a big home can be done in steps: `Stage` limits
the file types and how much one run embeds, and the report counts what is left.

Dedupe: a file whose sha256 equals an already indexed file's is not embedded again. Its path is kept
as an alias in `aliases-<collection>.json` next to the index (data dir); search results carry the
canonical file's real path and chunk_index. Aliases are not in the vector store, so `locus_read_file`
does not accept them yet (see the W1 hand-over note for what that would need).
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..policy import Matcher
from .embedder import Embedder
from .errors import as_embed_error
from .indexer import IndexStats, IndexStop, Logger, _stderr, file_sha256, index_file, iter_files, stop_message
from .retry import DEFAULT_RETRY, RetryPolicy
from .store import VectorStore

_MTIME_SAME = 1e-6
ORDERS = ("walk", "recent")


def parse_kinds(text: str | None) -> frozenset[str]:
    """ "md, .TXT" -> frozenset({".md", ".txt"}); empty -> empty."""
    parts = (p.strip().lower() for p in (text or "").split(","))
    return frozenset(p if p.startswith(".") else f".{p}" for p in parts if p)


@dataclass(frozen=True)
class Stage:
    """What one run may do. kinds: only these extensions (None = all); skip_kinds: never these;
    max_files / max_mb: stop embedding new files once this many files / megabytes were embedded;
    order: "walk" (folder order) or "recent" (newest modified first, across all roots, so a limit keeps
    the hot set); since_days: leave files not modified in the last N days for a later run;
    budget_seconds: stop starting new files once this much wall-clock time has passed (the file in
    progress is finished); with order="recent" this is the time-boxed hot-set pass."""

    kinds: frozenset[str] | None = None
    skip_kinds: frozenset[str] = frozenset()
    max_files: int | None = None
    max_mb: float | None = None
    order: str = "walk"
    since_days: float | None = None
    budget_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.order not in ORDERS:
            raise ValueError(f"unknown order {self.order!r}; expected one of {', '.join(ORDERS)}")

    def allows(self, ext: str) -> bool:
        return (self.kinds is None or ext in self.kinds) and ext not in self.skip_kinds


@dataclass
class MultiRootReport:
    stats: IndexStats = field(default_factory=IndexStats)
    duplicates: int = 0  # byte-identical to an indexed file: aliased, not embedded
    denied: int = 0  # files and folders the policy refused
    left_out: Counter[str] = field(default_factory=Counter)  # other skip reasons (unsupported_type, ...)
    deferred_kind: Counter[str] = field(default_factory=Counter)  # extension -> files outside this stage's kinds
    deferred_budget: int = 0  # files not looked at because the max-files / max-mb budget was used up
    deferred_since: int = 0  # files older than --since, left for a run without it
    walk_seconds: float | None = None  # --order recent: time spent listing files first, not counted in the budget
    deferred_time: int = 0  # files not started because the --budget-seconds wall-clock budget was spent
    budget_seconds: float | None = None  # the budget this run had, for the coverage line
    aliases: dict[str, list[str]] = field(default_factory=dict)  # canonical path -> alias paths

    @property
    def remaining(self) -> int:
        return sum(self.deferred_kind.values()) + self.deferred_budget + self.deferred_since + self.deferred_time


class AliasBook:
    """sha256 -> {canonical path, alias path -> mtime}, kept in a small JSON file beside the index."""

    def __init__(self, file: Path):
        self.file = file
        self.entries: dict[str, dict[str, Any]] = {}
        self.by_alias: dict[str, str] = {}
        try:
            self.entries = json.loads(file.read_text(encoding="utf-8")).get("hashes", {})
        except (OSError, ValueError):
            self.entries = {}
        for digest, entry in self.entries.items():
            for alias in entry.get("aliases", {}):
                self.by_alias[alias] = digest

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hashes": self.entries}), encoding="utf-8")
        os.replace(tmp, self.file)

    def canonical(self, digest: str) -> str | None:
        entry = self.entries.get(digest)
        return entry["path"] if entry else None

    def add_alias(self, digest: str, path: str, mtime: float) -> None:
        self.entries[digest]["aliases"][path] = mtime
        self.by_alias[path] = digest

    def drop_alias(self, path: str) -> None:
        digest = self.by_alias.pop(path, None)
        if digest in self.entries:
            self.entries[digest]["aliases"].pop(path, None)

    def set_canonical(self, digest: str, path: str) -> None:
        entry = self.entries.get(digest)
        if entry is None or entry["path"] != path:
            self.entries[digest] = {"path": path, "aliases": {}}

    def as_dict(self) -> dict[str, list[str]]:
        return {e["path"]: sorted(e["aliases"]) for e in self.entries.values() if e["aliases"]}


class _Run:
    """One staged pass: holds the collaborators so the per-file steps stay small."""

    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        settings: Settings,
        stage: Stage,
        book: AliasBook,
        log: Logger,
        retry: RetryPolicy,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.embedder, self.store, self.settings = embedder, store, settings
        self.stage, self.book, self.log, self.retry = stage, book, log, retry
        self.report = MultiRootReport(budget_seconds=stage.budget_seconds)
        self.clock = clock
        self.walk_started = clock()
        self.deadline = None if stage.budget_seconds is None else clock() + stage.budget_seconds
        self.embedded_bytes = 0
        self.states: dict[str, tuple[str | None, float | None]] | None = None
        days = stage.since_days
        self.cutoff = None if days is None else time.time() - days * 86400.0

    @property
    def stats(self) -> IndexStats:
        return self.report.stats

    def budget_used_up(self) -> bool:
        s = self.stage
        if s.max_files is not None and self.stats.files_indexed >= s.max_files:
            return True
        return s.max_mb is not None and self.embedded_bytes >= s.max_mb * 1024 * 1024

    def restart_clock_after_walk(self) -> None:
        """`--order recent` lists and stats every file before the first embed; that walk is not part of the budget."""
        now = self.clock()
        self.report.walk_seconds = now - self.walk_started
        if self.stage.budget_seconds is not None:
            self.deadline = now + self.stage.budget_seconds

    def time_used_up(self) -> bool:
        return self.deadline is not None and self.clock() >= self.deadline

    def load_states(self) -> None:
        """One bulk read of what is already indexed (path -> (hash, mtime)), so the per-file skip check costs a
        dict lookup instead of a Chroma `get` (6.5 ms each on a 70k-chunk collection). Stores without
        `file_states` keep the per-file lookups. The time budget starts after this read."""
        bulk = getattr(self.store, "file_states", None)
        if bulk is not None:
            self.states = bulk()
        if self.stage.budget_seconds is not None:
            self.deadline = self.clock() + self.stage.budget_seconds

    def _state(self, path: str) -> tuple[str | None, float | None]:
        if self.states is not None:
            return self.states.get(path, (None, None))
        return _state(self.store, path)

    def already_done(self, path: Path, mtime: float) -> bool:
        """Indexed (same modified time as recorded) or a still valid alias: nothing to do, nothing read."""
        key = str(path)
        digest = self.book.by_alias.get(key)
        if digest is not None:
            entry = self.book.entries[digest]
            if abs(entry["aliases"].get(key, -1.0) - mtime) <= _MTIME_SAME and self._state(entry["path"])[0] == digest:
                self.report.duplicates += 1
                return True
            self.book.drop_alias(key)
        recorded_hash, recorded_mtime = self._state(key)
        return recorded_hash is not None and recorded_mtime is not None and abs(recorded_mtime - mtime) <= _MTIME_SAME

    def try_alias(self, path: Path, digest: str, mtime: float) -> bool:
        canonical = self.book.canonical(digest)
        if canonical is None or canonical == str(path) or self._state(canonical)[0] != digest:
            return False
        self.book.add_alias(digest, str(path), mtime)
        self.report.duplicates += 1
        self.stats.files_skipped += 1
        return True

    def process(self, path: Path, root: Path) -> None:
        self.stats.files_seen += 1
        info = path.stat()
        if self.already_done(path, info.st_mtime):
            self.stats.files_skipped += 1
            return
        ext = path.suffix.lower()
        if not self.stage.allows(ext):
            self.report.deferred_kind[ext or "(none)"] += 1
            return
        if self.cutoff is not None and info.st_mtime < self.cutoff:
            self.report.deferred_since += 1
            return
        if self.budget_used_up():
            self.report.deferred_budget += 1
            return
        if self.time_used_up():
            self.report.deferred_time += 1
            return
        digest = file_sha256(path)
        if self.try_alias(path, digest, info.st_mtime):
            return
        before = self.stats.files_indexed
        index_file(path, root, self.embedder, self.store, self.settings, self.stats, self.log, self.retry)
        if self.states is not None:  # keep the snapshot true for this path (an alias check may ask about it later)
            self.states[str(path)] = _state(self.store, str(path))
        self.book.set_canonical(digest, str(path))
        if self.stats.files_indexed > before:
            self.embedded_bytes += info.st_size

    def guarded(self, path: Path, root: Path) -> bool:
        """Process one file; False when the whole run must stop (a failure every later file would repeat)."""
        try:
            self.process(path, root)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            self.stats.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            self.log(f"ERROR {path}: {exc}")
            typed = as_embed_error(exc)
            if typed is not None and typed.stops_run:
                message = stop_message(typed, root=root, indexed=self.stats.files_indexed, pending=0)
                self.stats.stop = IndexStop(error=typed, path=str(path), message=message)
                self.log(message)
                return False
        return True

    def on_skip(self, reason: str, _path: Path) -> None:
        if reason.startswith("denied_"):
            self.report.denied += 1
        else:
            self.report.left_out[reason] += 1


def _state(store: VectorStore, path: str) -> tuple[str | None, float | None]:
    file_state = getattr(store, "file_state", None)
    return file_state(path) if file_state is not None else (store.file_hash(path), None)


def _newest_first(roots: Iterable[Path], settings: Settings, matcher: Matcher, run: _Run) -> list[tuple[Path, Path]]:
    """Every eligible (path, root), newest modified first. Needs one stat per file up front (the walk
    already lists them); ties and unreadable files sort by path so the order is the same on every run."""
    found: list[tuple[float, Path, Path]] = []
    for root in roots:
        root = Path(root)
        for path in iter_files(root, settings, run.on_skip, matcher):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            found.append((mtime, path, root))
    found.sort(key=lambda item: (-item[0], str(item[1])))
    return [(path, root) for _, path, root in found]


def index_roots(
    roots: Iterable[Path],
    embedder: Embedder,
    store: VectorStore,
    settings: Settings,
    matcher: Matcher,
    stage: Stage | None = None,
    log: Logger = _stderr,
    retry: RetryPolicy = DEFAULT_RETRY,
    clock: Callable[[], float] = time.monotonic,
) -> MultiRootReport:
    """Index every eligible file under *roots* into *store*, within *stage*'s limits."""
    book = AliasBook(settings.data_dir / f"aliases-{settings.collection}.json")
    run = _Run(embedder, store, settings, stage or Stage(), book, log, retry, clock)
    try:
        run.load_states()
        if run.stage.order == "recent":
            queue = _newest_first(roots, settings, matcher, run)
            run.restart_clock_after_walk()
        else:
            queue = ((p, Path(root)) for root in roots for p in iter_files(Path(root), settings, run.on_skip, matcher))
        for path, root in queue:
            if not run.guarded(path, root):
                break
    finally:
        book.save()
    run.report.aliases = book.as_dict()
    log(run.stats.summary())
    return run.report


def render_report(report: MultiRootReport) -> str:
    """Counts only: no file or folder name, so it can be pasted into a chat or a note."""
    s = report.stats
    lines = [
        f"Indexed {s.files_indexed:,} file(s), {s.chunks:,} chunks; {s.files_skipped:,} unchanged or aliased; "
        f"{len(s.errors):,} error(s).",
        f"Duplicates (byte-identical, embedded once): {report.duplicates:,}. "
        f"Refused by the deny-list: {report.denied:,}.",
    ]
    if report.left_out:
        lines.append("Left out: " + ", ".join(f"{k} {v:,}" for k, v in sorted(report.left_out.items())) + ".")
    if s.pdf_no_text or s.pdf_truncated:
        lines.append(
            f"PDFs with no extractable text (scanned, not indexed): {s.pdf_no_text:,}. "
            f"PDFs cut at the page or time limit: {s.pdf_truncated:,}."
        )
    if s.stop is not None:
        lines.append(f"STOPPED EARLY ({s.stop.kind}); re-run the same command to continue.")
    lines.append(f"Remaining for later runs: {report.remaining:,} file(s)")
    if report.remaining:
        parts = [f"{ext} {n:,}" for ext, n in sorted(report.deferred_kind.items(), key=lambda kv: -kv[1])]
        if report.deferred_budget:
            parts.append(f"not reached (limit) {report.deferred_budget:,}")
        if report.deferred_since:
            parts.append(f"older than --since {report.deferred_since:,}")
        if report.deferred_time:
            parts.append(f"not reached (time budget) {report.deferred_time:,}")
        lines[-1] += " (" + ", ".join(parts) + "). Run again with other --kinds or a higher limit."
    else:
        lines[-1] += "."
    if report.budget_seconds is not None:
        spent = " spent" if report.deferred_time else " not used up"
        lines.append(
            f"Coverage ({report.budget_seconds:g} s budget{spent}): {s.files_indexed:,} file(s) indexed this run, "
            f"{report.remaining:,} eligible file(s) remaining; the rest is not indexed yet."
        )
        if report.walk_seconds is not None:
            lines[-1] += f" (Listing the files first took {report.walk_seconds:.0f} s, not counted in the budget.)"
    return "\n".join(lines)
