"""Index-on-demand: a bounded freshness step that `locus serve` runs before it answers a search.

A cheap mtime scan of the allowed roots (policy roots, else the folders already indexed) finds files that were
added or changed since they were indexed. Only those are touched: an unchanged file costs one `stat`, never an
embedding. The whole step has a time budget; when it runs out the result says how many changed files are not indexed
yet (counts only) and the next call continues. It runs inside the caller's request: no thread, no background work, and
`locus watch` is not involved.

On a root of tens of thousands of files one call cannot look at everything, so the scan is resumable:
it walks in a fixed order, stops at its share of the budget and remembers where, and the next call carries on from
there; changed files are indexed newest first, and the ones that did not fit in time are remembered too. A new file is
therefore found within one scan cycle (a few calls on a big root), and then indexed before older changed files.

Files that are gone, and indexed files a deny rule now matches, are found by a second, exact pass (`iter_files` with
the policy matcher on every file). It is the expensive one, so it runs only when a whole scan cycle fit in one call and
the exact pass is projected to fit in the time left; on a root this big it does not, and `locus index <folder> --prune`
removes rows of deleted files and of files a deny rule now matches.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..policy import Matcher, PolicyError, load_policy
from ..preflight import needs_consent
from .embedder import Embedder
from .errors import as_embed_error
from .indexer import IndexStats, file_sha256, index_file, iter_files
from .multiroot import AliasBook
from .scan import Position, StatScan, position_from_json, position_to_json
from .store import VectorStore

Clock = Callable[[], float]

BUDGET_ENV = "LOCUS_FRESHNESS_BUDGET_S"
DEFAULT_BUDGET_S = 2.0
# Chroma hands some stored floats back one unit in the last place off (see indexer._MTIME_ROUND_TRIP).
_MTIME_SLACK = 1e-6
# The scan may use this share of the budget; the rest is kept for indexing, so a huge tree cannot starve it. The scan
# is resumable, so what it does not reach in one call it reaches in the next.
_SCAN_SHARE = 0.5
# The exact pass (iter_files with the policy matcher on every file) runs only when a whole scan cycle fit in one call
# and the pass is projected to fit in the time left. Its per-file cost without the matcher is about 15 us (MEASURED on
# a synthetic tree, 2026-11-02); the matcher's share varies too much to assume (55 us per file there, about 340 us on
# the maintainer's machine, 24.4 s for 62,495 files in all), so it is timed on a few indexed paths.
_WALK_S_PER_FILE = 15e-6
_COST_SAMPLE = 16
# Changed files that were found but not indexed in time, newest first, that the next call remembers (paths and times).
_PENDING_KEEP = 200
# Reading the index (a reopen after another process wrote to it can take seconds) must not use up the whole scan share
# and leave the scan with no time at all, call after call: it gets at least this long, within the overall budget.
_MIN_SCAN_S = 0.2
# Deleting index rows on partial evidence is the one thing here that loses work (re-indexing takes hours): a root that
# lost most of its files at once (an empty mount point, a volume that remounted late) keeps its rows, and one pass
# removes at most this many files. Roots with fewer recorded files than _GUARD_MIN_FILES are too small to tell.
_MAX_REMOVALS_PER_PASS = 200
_GUARD_MIN_FILES = 3


@dataclass
class FreshnessReport:
    indexed: int = 0  # files embedded and written this call
    removed: int = 0  # files dropped from the index (deleted, or no longer allowed)
    pending: int = 0  # changed files found but not indexed yet (budget, or a run-stopping embed error)
    errors: int = 0  # files that failed on their own and were skipped
    scan_complete: bool = True  # False: the budget ended the scan (the next call resumes it), or a folder is unreadable
    scanned: int = 0  # files whose modification time this call read
    removals_refused: int = 0  # vanished files whose rows were kept (most of a root gone at once, or the per-pass cap)
    kept_denied: int = 0  # indexed files a deny rule matches now: kept, not refreshed
    hosted_off: bool = False  # the embedder is hosted: freshness did nothing at all

    @property
    def cut_short(self) -> bool:
        return self.pending > 0 or not self.scan_complete

    @property
    def has_note(self) -> bool:
        return self.cut_short or bool(self.removals_refused or self.kept_denied or self.hosted_off)


def budget_from_env(env: Mapping[str, str] | None = None) -> float:
    """Seconds the step may use: LOCUS_FRESHNESS_BUDGET_S (default 2; 0 or less switches it off)."""
    raw = (os.environ if env is None else env).get(BUDGET_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_BUDGET_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_BUDGET_S


def _under(path: str, roots: list[str]) -> bool:
    return any(path.startswith(r + os.sep) for r in roots)


def _mtime_differs(recorded: float | None, path: Path) -> bool | None:
    """True when *path* must be looked at again; None when it cannot be stat'ed."""
    try:
        current = path.stat().st_mtime
    except OSError:
        return None
    return recorded is None or abs(current - recorded) > _MTIME_SLACK


class _SeenNothing:
    """Files that were parsed and produced no chunks (empty, scanned PDF, corrupt), path -> mtime when that happened.
    They have no row in the index, so without this every pass would treat them as changed and parse them again. A
    small JSON file beside the index; it holds paths and times only. Unreadable or missing means 'no memory'."""

    def __init__(self, file: Path):
        self.file = file
        self.entries: dict[str, float] = {}
        self.dirty = False
        try:
            raw = json.loads(file.read_text(encoding="utf-8")).get("seen", {})
            self.entries = {str(k): float(v) for k, v in raw.items()}
        except (OSError, ValueError, AttributeError, TypeError):
            self.entries = {}

    def covers(self, key: str, mtime: float) -> bool:
        recorded = self.entries.get(key)
        return recorded is not None and abs(mtime - recorded) <= _MTIME_SLACK

    def add(self, key: str, path: Path) -> None:
        try:
            self.entries[key] = path.stat().st_mtime
        except OSError:
            return
        self.dirty = True

    def forget_except(self, keep: set[str], roots: list[str]) -> None:
        for key in [k for k in self.entries if _under(k, roots) and k not in keep]:
            del self.entries[key]
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seen": self.entries}), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            pass  # no memory next time: the cost is a re-parse, never a wrong answer


class _ScanState:
    """Where the last scan stopped, and the newest changed files it found but could not index in time: paths, times and
    a folder position, no content. A small JSON file beside the index. Unreadable or missing means 'start at the top
    with nothing remembered': the cost is a slower first cycle, never a wrong answer."""

    def __init__(self, file: Path):
        self.file = file
        self.root: str | None = None  # the root the scan stopped in; None: the last cycle ran to its end
        self.at: Position = ()
        self.pending: list[tuple[str, float, str]] = []  # (path, mtime, root)
        self.dirty = False
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
            cursor = raw.get("cursor") or {}
            root = cursor.get("root")
            self.root = root if isinstance(root, str) else None
            self.at = position_from_json(cursor.get("at")) if self.root is not None else ()
            self.pending = [(str(p), float(m), str(r)) for p, m, r in raw.get("pending", [])]
        except (OSError, ValueError, AttributeError, TypeError):
            self.root, self.at, self.pending = None, (), []

    def resume(self, root_texts: list[str]) -> tuple[int, Position]:
        """Which root of *root_texts* to start in and where in it."""
        if self.root is not None and self.root in root_texts:
            return root_texts.index(self.root), self.at
        return 0, ()

    def move(self, root: str | None, at: Position) -> None:
        if (root, at) != (self.root, self.at):
            self.root, self.at, self.dirty = root, at, True

    def remember(self, pending: list[tuple[str, float, str]]) -> None:
        if pending != self.pending:
            self.pending, self.dirty = pending, True

    def save(self) -> None:
        if not self.dirty:
            return
        cursor = {"root": self.root, "at": position_to_json(self.at)} if self.root is not None else {}
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"cursor": cursor, "pending": self.pending}), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            pass  # no memory next time: the cost is a rescan from the top, never a wrong answer


def freshen(
    roots: list[Path],
    matcher: Matcher | None,
    embedder: Embedder,
    store: VectorStore,
    settings: Settings,
    budget_s: float = DEFAULT_BUDGET_S,
    clock: Clock = time.monotonic,
) -> FreshnessReport:
    """Bring the index of *roots* up to date within *budget_s* seconds (measured on *clock*)."""
    report = FreshnessReport()
    if budget_s <= 0:
        return report
    if needs_consent(settings):
        # A hosted embedder sends chunk text off this computer: no walk, embedding or deletion.
        report.hosted_off = True
        return report
    start = clock()
    scan_deadline, deadline = start + budget_s * _SCAN_SHARE, start + budget_s
    recorded = store.file_mtimes()  # type: ignore[attr-defined]  # one metadata read: path -> mtime at indexing
    for outdated in getattr(store, "outdated_paths", set)():  # an older parser's rows: indexed again, file unchanged
        if outdated in recorded:
            recorded[outdated] = None
    live_roots = [Path(r) for r in roots if Path(r).is_dir()]  # a missing root (unmounted volume) deletes nothing
    root_texts = [str(r) for r in live_roots]
    nothing = _SeenNothing(settings.data_dir / f"freshness-seen-{settings.collection}.json")
    # byte-identical copies that multiroot deduplicated: embedding them here would undo that
    aliases = AliasBook(settings.data_dir / f"aliases-{settings.collection}.json")
    state = _ScanState(settings.data_dir / f"freshness-scan-{settings.collection}.json")
    unreadable = False
    scan_deadline = max(scan_deadline, min(clock() + _MIN_SCAN_S, deadline))

    def on_skip(reason: str, _path: Path) -> None:
        nonlocal unreadable
        # a folder we could not list, or a file we could not stat: its rows are not proof that anything is gone
        unreadable = unreadable or reason in ("unreadable_dir", "unreadable")

    def alias_unchanged(key: str, mtime: float) -> bool:
        digest = aliases.by_alias.get(key)
        try:
            alias_mtime = aliases.entries[digest]["aliases"].get(key) if digest is not None else None
        except KeyError:
            return False
        return alias_mtime is not None and abs(mtime - alias_mtime) <= _MTIME_SLACK

    def is_copy_of_indexed_content(key: str, path: Path) -> bool:
        digest = aliases.by_alias.get(key)  # touched since it was recorded: still a copy unless the bytes differ
        try:
            return digest is not None and file_sha256(path) == digest
        except OSError:
            return False

    # 1. Discovery: a resumable stat-only walk from where the last call stopped. A policy question is asked about
    #    folders only; single files wait until they turn out to be new or changed (below).
    candidates: dict[str, tuple[float, Path]] = {}  # path -> (its mtime, its root)
    first, at = state.resume(root_texts)
    from_the_top = first == 0 and not at
    stopped_root: str | None = None
    stopped_at: Position = ()

    def scan_time_is_up() -> bool:
        return clock() >= scan_deadline

    for i in range(first, len(live_roots)):
        scan = StatScan(live_roots[i], settings, matcher, on_skip, scan_time_is_up, at if i == first else ())
        for key, mtime in scan.files():
            if key in candidates:
                continue
            indexed_mtime = recorded.get(key)
            if indexed_mtime is not None:
                if abs(mtime - indexed_mtime) <= _MTIME_SLACK:
                    continue
            elif nothing.covers(key, mtime) or alias_unchanged(key, mtime):
                continue
            candidates[key] = (mtime, live_roots[i])
        report.scanned += scan.examined
        if not scan.finished:
            stopped_root, stopped_at = root_texts[i], scan.resume_at or ()
            break
    state.move(stopped_root, stopped_at)
    cycle_done = stopped_root is None
    for key, mtime, root_text in state.pending:  # found earlier, not indexed in time: still wanted, if still true
        if key not in candidates and root_text in root_texts:
            candidates[key] = (mtime, live_roots[root_texts.index(root_text)])

    # 2. Index what changed, newest first: a file made a minute ago before a backlog from last year.
    stats = IndexStats()
    todo = sorted(candidates.items(), key=lambda item: (-item[1][0], item[0]))
    left: list[tuple[str, float, str]] = []  # what the next call is told to look at first

    def remember(rest: list[tuple[str, tuple[float, Path]]]) -> None:
        left.extend((key, mtime, str(root)) for key, (mtime, root) in rest[:_PENDING_KEEP])

    for n, (key, (_, root)) in enumerate(todo):
        if clock() >= deadline:
            report.pending = len(todo) - n
            remember(todo[n:])
            break
        path = Path(key)
        indexed_mtime = recorded.get(key)
        if not _mtime_differs(indexed_mtime, path):  # None: gone since; False: indexed meanwhile, or put back
            continue
        if matcher is not None and not matcher.decide(path).allowed:
            continue  # a file a deny rule matches is not indexed, and a changed one is not refreshed
        if indexed_mtime is None and is_copy_of_indexed_content(key, path):
            continue
        try:
            index_file(path, root, embedder, store, settings, stats, log=lambda _msg: None)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the step
            report.errors += 1
            typed = as_embed_error(exc)
            if typed is not None and typed.stops_run:  # every remaining file would fail the same way
                report.pending = len(todo) - n - 1
                remember(todo[n:])
                break
            nothing.add(key, path)  # failed on its own (corrupt file): not again until it changes
            continue
        if store.file_hash(key) is None:  # parsed, nothing to index (empty, scanned PDF): do not parse it again
            nothing.add(key, path)
    state.remember(left)

    # 3. Gone files and rows a deny rule now matches need the exact pass. It proves something only when it covers
    #    every root, so it runs only after a whole cycle fit in this call, and only when it is projected to fit.
    if (
        cycle_done
        and from_the_top
        and not unreadable
        and _exact_pass_fits(report.scanned, matcher, recorded, deadline - clock())
    ):
        seen: set[str] = set()
        exact_done = True
        for root in live_roots:
            for path in iter_files(root, settings, on_skip, matcher):
                if clock() >= deadline:
                    exact_done = False
                    break
                seen.add(str(path))
            if not exact_done:
                break
        if exact_done and not unreadable:  # only a finished, fully readable walk proves a file is gone
            nothing.forget_except(seen, root_texts)
            _remove_vanished(recorded, seen, root_texts, matcher, store, report)
    report.scan_complete = cycle_done and not unreadable
    nothing.save()
    state.save()
    report.indexed = stats.files_indexed
    return report


def _exact_pass_fits(files: int, matcher: Matcher | None, indexed_paths: Iterable[str], seconds_left: float) -> bool:
    """Whether one exact pass over *files* files is projected to finish within *seconds_left*. Timed in real time, not
    on the injected clock: it is a prediction about this machine, not part of the budget."""
    per_file = _WALK_S_PER_FILE
    sample = [Path(p) for p in itertools.islice(indexed_paths, _COST_SAMPLE)]
    if matcher is not None and sample:
        began = time.perf_counter()
        for path in sample:
            matcher.decide(path)
        per_file += (time.perf_counter() - began) / len(sample)
    return files * per_file <= seconds_left


def _remove_vanished(
    recorded: Mapping[str, float | None],
    seen: set[str],
    root_texts: list[str],
    matcher: Matcher | None,
    store: VectorStore,
    report: FreshnessReport,
) -> None:
    """Drop rows of files that are gone from disk, with brakes: a file a deny rule matches now but that still exists is
    kept and counted (freshness never deletes because of a rule; `--prune` does); most of a root vanishing at
    once is more likely a mount problem than a clean-up; one pass removes at most _MAX_REMOVALS_PER_PASS."""
    by_root: dict[str, list[str]] = {r: [] for r in root_texts}
    gone_by_root: dict[str, list[str]] = {r: [] for r in root_texts}
    for path in recorded:
        owner = max((r for r in root_texts if path.startswith(r + os.sep)), key=len, default=None)
        if owner is None:
            continue
        by_root[owner].append(path)
        if path not in seen:
            gone_by_root[owner].append(path)
    budget = _MAX_REMOVALS_PER_PASS
    for root_text in root_texts:
        gone = sorted(gone_by_root[root_text])
        kept = {p for p in gone if _denial(matcher, p) is not None and os.path.lexists(p)}
        report.kept_denied += len(kept)
        vanished = [p for p in gone if p not in kept]
        if len(by_root[root_text]) >= _GUARD_MIN_FILES and len(vanished) * 2 > len(by_root[root_text]):
            report.removals_refused += len(vanished)
            vanished = []
        for path in vanished:
            if budget <= 0:
                report.removals_refused += 1
                continue
            store.delete_path(path)
            report.removed += 1
            budget -= 1


def _denial(matcher: Matcher | None, path: str) -> str | None:
    """The deny reason the matcher gives *path* now, or None when it is allowed (or there is no matcher)."""
    if matcher is None:
        return None
    decision = matcher.decide(Path(path))
    return None if decision.allowed else decision.reason


def render_freshness_note(report: FreshnessReport, budget_s: float) -> str | None:
    """A visible last line for a result when the step could not finish (counts only), else None."""
    if not report.has_note:
        return None
    parts = ["Freshness note:"]
    if report.pending:
        parts.append(
            f"{report.pending} changed file(s) not yet indexed (the {budget_s:g} s check budget ran out or "
            "embedding stopped); results above may miss them. The next search continues."
        )
    if not report.scan_complete:
        parts.append(
            f"the scan of your folders had not finished within {budget_s:g} s (or a folder could not be read), "
            "so files added or changed in the part not scanned may not be searchable yet; the next search "
            "continues where this one stopped."
        )
    if report.removals_refused:
        parts.append(
            f"{report.removals_refused} indexed file(s) no longer found were kept in the index (removing "
            "most of a folder at once is refused, in case a drive is unmounted); "
            "`locus index <folder> --prune` removes them for real."
        )
    if report.kept_denied:
        parts.append(
            f"{report.kept_denied} indexed file(s) now match a deny rule and were kept; remove them with an "
            "explicit `locus index <folder> --prune` on the folder they are in (it applies the same deny rules)."
        )
    if report.hosted_off:
        return (
            "Freshness note: automatic freshness is off because the embedder is hosted (it would send file text off "
            "this computer); `locus index <folder>` refreshes the index explicitly."
        )
    return " ".join(parts)


def serve_roots_and_matcher(
    store: VectorStore, settings: Settings, home: Path | None = None
) -> tuple[list[Path], Matcher | None]:
    """The roots to keep fresh and the matcher that decides what is allowed in them. Roots come from the policy file
    (`<data dir>/policy.toml`); with none configured, from the folders already indexed. Raises PolicyError."""
    home = home or Path.home()
    policy = load_policy(settings.data_dir / "policy.toml")
    roots = [Path(r).expanduser() for r in policy.roots]
    if not roots:
        indexed_roots = getattr(store, "indexed_roots", None)
        roots = [Path(r) for r in (indexed_roots() if indexed_roots else [])]
    if not roots:
        return [], None
    policy = dataclasses.replace(policy, roots=tuple(str(r) for r in roots))
    return roots, Matcher(policy, home)


def freshen_for_serve(
    embedder: Embedder,
    store: VectorStore,
    settings: Settings | None,
    env: Mapping[str, str] | None = None,
    clock: Clock = time.monotonic,
) -> str | None:
    """What `locus_search` calls: freshen, and return the note to append (None when there is nothing to say).
    Never raises: a failure is logged to stderr and the search goes on with the index as it is."""
    budget_s = budget_from_env(env)
    if settings is None or budget_s <= 0 or not hasattr(store, "file_mtimes"):
        return None
    try:
        roots, matcher = serve_roots_and_matcher(store, settings)
        if not roots:
            return None
        report = freshen(roots, matcher, embedder, store, settings, budget_s, clock)
    except PolicyError:
        return "Freshness note: skipped, because the policy file has a problem (`locus index --dry-run` names it)."
    except Exception as exc:  # noqa: BLE001 - freshness must never break a search
        print(f"locus_mcp: freshness step failed: {type(exc).__name__}", file=sys.stderr)
        return None
    return render_freshness_note(report, budget_s)
