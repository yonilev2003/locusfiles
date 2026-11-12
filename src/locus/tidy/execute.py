"""Execute, recover and undo a plan (draft ADR-082 items 2 and 6).

Execution re-checks every row against its fingerprint and the root, journals `intent` before the move and
`done` after it, and never overwrites. Approval is the caller's job and happens in a channel the model cannot
call: `execute_plan` takes the row indexes the user approved (None = all rows)."""

from __future__ import annotations

import os
import stat as stat_module
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .fsops import rename_exclusive
from .journal import Journal
from .plan import Plan, Row


@dataclass
class ExecuteResult:
    run: str
    moved: list[int] = field(default_factory=list)
    skipped: dict[int, str] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)


@dataclass
class UndoResult:
    restored: list[int] = field(default_factory=list)
    not_restored: dict[int, str] = field(default_factory=dict)
    removed_dirs: list[str] = field(default_factory=list)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _row_problem(row: Row, root: Path) -> str | None:
    """Why this row must not run now, or None. A row is proposed against one exact file."""
    src, dst = Path(row.src), Path(row.dst)
    if src.parent != root:
        return "source is not a top-level file of the root"
    if not _under(dst.parent, root) or dst.parent == root:
        return "destination is outside the root"
    try:
        st = os.lstat(src)
    except OSError:
        return "source is gone"
    if not stat_module.S_ISREG(st.st_mode) or st.st_nlink > 1:
        return "source is no longer a plain single-name file"
    if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (row.dev, row.ino, row.size, row.mtime_ns):
        return "source changed since the plan was made"
    if os.path.lexists(dst):
        return "destination exists"
    return None


def execute_plan(plan: Plan, journal: Journal, approved: set[int] | None = None) -> ExecuteResult:
    root = Path(plan.root).resolve(strict=True)
    result = ExecuteResult(run=uuid.uuid4().hex[:12])
    for index, row in enumerate(plan.rows):
        if approved is not None and index not in approved:
            continue
        problem = _row_problem(row, root)
        if problem is not None:
            result.skipped[index] = problem
            journal.append({"run": result.run, "row": index, "state": "skipped", "why": problem, "src": row.src})
            continue
        dst_parent = Path(row.dst).parent
        created = []
        probe = dst_parent
        while probe != root and not probe.exists():
            created.append(str(probe))
            probe = probe.parent
        journal.append(
            {
                "run": result.run,
                "row": index,
                "state": "intent",
                "src": row.src,
                "dst": row.dst,
                "ino": row.ino,
                "dev": row.dev,
                "size": row.size,
                "created_dirs": created[::-1],
            }
        )
        try:
            dst_parent.mkdir(parents=True, exist_ok=True)
            rename_exclusive(row.src, row.dst)
        except OSError as exc:
            result.failed[index] = f"{type(exc).__name__}: {exc.strerror or exc}"
            why = result.failed[index]
            journal.append({"run": result.run, "row": index, "state": "failed", "why": why, "src": row.src})
            continue
        journal.append({"run": result.run, "row": index, "state": "done", "src": row.src, "dst": row.dst})
        result.moved.append(index)
    return result


def recover(journal: Journal) -> dict[str, list[str]]:
    """Settle rows whose last record is `intent` (the process died around the move). Decided from the disk:
    destination present and source gone = the move happened (record `done`); source present and destination
    absent = it did not (record `failed`). Anything else is reported and left for a person."""
    report: dict[str, list[str]] = {"finished": [], "rolled_back": [], "needs_a_person": []}
    for (run, index), rec in sorted(journal.last_state().items()):
        if rec["state"] != "intent":
            continue
        src_here, dst_here = os.path.lexists(rec["src"]), os.path.lexists(rec["dst"])
        if dst_here and not src_here:
            journal.append(
                {"run": run, "row": index, "state": "done", "src": rec["src"], "dst": rec["dst"], "recovered": True}
            )
            report["finished"].append(rec["dst"])
        elif src_here and not dst_here:
            journal.append(
                {
                    "run": run,
                    "row": index,
                    "state": "failed",
                    "why": "interrupted before the move",
                    "src": rec["src"],
                    "recovered": True,
                }
            )
            report["rolled_back"].append(rec["src"])
        else:
            report["needs_a_person"].append(f"{rec['src']} -> {rec['dst']}")
    return report


def undo(journal: Journal, run: str | None = None) -> UndoResult:
    """Move every `done` row of *run* (default: the latest run) back, last move first. A row is restored only
    if its destination is still the same file (device, inode, size) and the source name is free; otherwise it
    is reported and left alone. Folders this run created are removed only if they are empty."""
    result = UndoResult()
    state = journal.last_state()
    runs = [r for (r, _) in state]
    if not runs:
        return result
    run = run or [rec["run"] for rec in journal.records() if "row" in rec][-1]
    intents = {}
    for rec in journal.records():
        if rec.get("run") == run and rec.get("state") == "intent":
            intents[rec["row"]] = rec
    for index in sorted((i for (r, i), rec in state.items() if r == run and rec["state"] == "done"), reverse=True):
        intent = intents[index]
        src, dst = intent["src"], intent["dst"]
        try:
            st = os.lstat(dst)
        except OSError:
            result.not_restored[index] = "moved file is gone from its destination"
            continue
        if (st.st_dev, st.st_ino, st.st_size) != (intent["dev"], intent["ino"], intent["size"]):
            result.not_restored[index] = "the file at the destination is not the one that was moved"
            continue
        if os.path.lexists(src):
            result.not_restored[index] = "the original name is occupied now"
            continue
        try:
            rename_exclusive(dst, src)
        except OSError as exc:
            result.not_restored[index] = f"{type(exc).__name__}: {exc.strerror or exc}"
            continue
        journal.append({"run": run, "row": index, "state": "undone", "src": src, "dst": dst})
        result.restored.append(index)
    for index in sorted(intents, reverse=True):
        for directory in reversed(intents[index].get("created_dirs", [])):
            try:
                os.rmdir(directory)  # only succeeds when empty
                result.removed_dirs.append(directory)
            except OSError:
                pass
    return result
