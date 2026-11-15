"""A stat-first, resumable walk for the serve-time freshness step.

`indexer.iter_files` is the single definition of what may be indexed, and it asks the policy matcher about every
single file before anything else: on a synthetic tree that was 55 of 69 microseconds per file, and a pass over about
62,000 files took 24 s on the maintainer's machine. That is right for `locus index`, which then reads and embeds
what the walk yields. It is the wrong price for a step that only wants to know which files changed since they were
indexed.

This walk applies the checks that cost nothing first (a name test, the extension, the folder entry's own type), then
one `stat` per surviving file, and leaves the per-file policy question to the caller, who asks it only about files
that turned out to be new or changed. Folders are pruned exactly as `iter_files` prunes them (SKIP_DIRS, hidden, the
matcher's folder verdict, symlinked folders are not entered), so a denied folder is never entered. What it yields is
therefore `iter_files(...)` minus the matcher's verdict on single files (tests/test_stat_scan.py checks that).

The order is that of `iter_files`: the files of a folder, sorted by name, then its subfolders, sorted, depth first.
That fixed order is what makes the walk resumable: when `stop()` says time is up it records a `Position` (where to
continue, inclusive), and a later call with `resume=` that position carries on without looking at anything before it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

from ..config import SKIP_DIRS, Settings
from ..policy import Matcher
from .indexer import SkipHook

# () is the start of a folder. ((0, "b.md"),) is the file b.md of this folder. ((1, "sub"), (0, "b.md")) is the file
# b.md of the subfolder sub. ((1, "sub"),) is the start of sub. Files come before subfolders at every level.
Position = tuple[tuple[int, str], ...]

_MAX_DEPTH = 150  # folders nest as generators: deeper than this is reported as unreadable, not a stack overflow


def position_to_json(position: Position) -> list[list[int | str]]:
    return [[kind, name] for kind, name in position]


def position_from_json(raw: object) -> Position:
    """The position in *raw*, or () (the start) when it is not a well-formed one."""
    if not isinstance(raw, list):
        return ()
    out: list[tuple[int, str]] = []
    for item in raw:
        if not (isinstance(item, list) and len(item) == 2 and item[0] in (0, 1) and isinstance(item[1], str)):
            return ()
        out.append((int(item[0]), item[1]))
    return tuple(out)


def _by_name(entry: os.DirEntry[str]) -> str:
    return entry.name


class StatScan:
    """Walk *root*, yield `(path, mtime)` of every file that passes the cheap eligibility checks.

    `stop()` is asked before every file and every folder (not about the folders it walks back down through to reach a
    resumed position); when it returns True the walk ends and `resume_at` holds the position to continue from, at the
    file or folder it was about to look at. When the walk runs to its end `finished` is True and `resume_at` is None.
    `on_skip` only hears about the two failures that matter to a caller deciding whether a pass proved anything:
    "unreadable_dir" (a folder the OS would not list) and "unreadable" (a file that could not be stat'ed).
    """

    def __init__(
        self,
        root: Path,
        settings: Settings,
        matcher: Matcher | None,
        on_skip: SkipHook | None,
        stop: Callable[[], bool],
        resume: Position = (),
    ) -> None:
        self.root = Path(root).resolve()  # one spelling per folder, as iter_files does
        self.resume_at: Position | None = None
        self.finished = False
        self.examined = 0  # files that passed the cheap checks, i.e. the ones whose mtime was read
        self._extensions = frozenset(settings.extensions)
        self._max_bytes = int(settings.max_file_mb * 1024 * 1024)
        self._matcher, self._on_skip, self._stop, self._resume = matcher, on_skip, stop, resume
        self._halted = False

    def files(self) -> Iterator[tuple[str, float]]:
        if self._matcher is not None:
            verdict = self._matcher.decide(self.root, is_dir=True)
            if not verdict.allowed:
                self._skip(f"denied_{verdict.reason}", self.root)
                self.finished = True
                return
        yield from self._folder(str(self.root), (), self._resume)
        self.finished = not self._halted

    def _skip(self, reason: str, path: Path) -> None:
        if self._on_skip is not None:
            self._on_skip(reason, path)

    def _halt(self, position: Position) -> None:
        self._halted = True
        self.resume_at = position

    def _folder(self, dirpath: str, prefix: Position, frm: Position) -> Iterator[tuple[str, float]]:
        if len(prefix) > _MAX_DEPTH:
            self._skip("unreadable_dir", Path(dirpath))
            return
        try:
            with os.scandir(dirpath) as it:
                entries = sorted(it, key=_by_name)
        except OSError as err:
            self._skip("unreadable_dir", Path(err.filename or dirpath))
            return
        files: list[os.DirEntry[str]] = []
        folders: list[os.DirEntry[str]] = []
        for entry in entries:
            try:
                (folders if entry.is_dir() else files).append(entry)  # like os.walk: a symlink to a folder is a folder
            except OSError:
                files.append(entry)
        kind, first = frm[0] if frm else (0, "")
        if kind == 0:
            for entry in files:
                if entry.name < first:
                    continue
                if self._stop():
                    self._halt((*prefix, (0, entry.name)))
                    return
                item = self._file(entry)
                if item is not None:
                    yield item
            first = ""  # every subfolder follows
        for entry in folders:
            if entry.name < first:
                continue
            on_the_way = kind == 1 and entry.name == first and len(frm) > 1  # walking down to where we stopped
            if not on_the_way and self._stop():  # going back down is not new work: it must not use up the slice
                self._halt((*prefix, (1, entry.name)))
                return
            if not self._may_enter(entry):
                continue
            inside = frm[1:] if (kind == 1 and entry.name == first) else ()
            yield from self._folder(entry.path, (*prefix, (1, entry.name)), inside)
            if self._halted:
                return

    def _may_enter(self, entry: os.DirEntry[str]) -> bool:
        name = entry.name
        if name in SKIP_DIRS or name.startswith("."):
            return False
        try:
            if entry.is_symlink():  # os.walk lists a symlinked folder but never enters it
                return False
        except OSError:
            return False
        if self._matcher is not None and not self._matcher.decide(Path(entry.path), is_dir=True).allowed:
            return False
        return True

    def _file(self, entry: os.DirEntry[str]) -> tuple[str, float] | None:
        name = entry.name
        if name.startswith("."):
            return None
        dot = name.rfind(".")
        if dot < 0 or name[dot:].lower() not in self._extensions:
            return None
        try:
            if entry.is_symlink():  # never followed: a link may point anywhere on disk
                return None
            st = entry.stat(follow_symlinks=False)
        except OSError:
            self._skip("unreadable", Path(entry.path))
            return None
        if st.st_size > self._max_bytes:
            return None
        self.examined += 1
        return entry.path, st.st_mtime
