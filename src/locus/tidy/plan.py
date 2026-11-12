"""Build, save and load a plan (draft ADR-082 items 1, 6, 7). Building reads directory entries and stats;
it never opens a file's contents except to hash same-size candidates for the duplicate flag."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .labels import FOLDERS, MONTH_BUCKETS
from .rules import UtiLookup, classify
from .safety import display_name, unsafe_reason

MAX_OPS = 200
MIN_SECONDS_BETWEEN_PLANS = 3600
SCHEMA = 1


class PlanLimit(Exception):
    """A plan limit (one pending plan, one plan per hour) forbids saving a new plan."""


@dataclass
class Row:
    """One proposed move. dev/ino/size/mtime_ns are the fingerprint execution re-checks."""

    src: str
    dst: str
    label: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    duplicate_of: str | None = None  # info only: an identical file is flagged, never removed


@dataclass
class Plan:
    root: str
    created_at: float
    rows: list[Row] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)  # {name (display-safe), reason}
    kept: list[str] = field(default_factory=list)  # display-safe names labelled "other": they stay
    truncated: bool = False  # more than MAX_OPS candidates: the rest wait for the next plan
    schema: int = SCHEMA

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True, ensure_ascii=True)


def _free_name(folder: Path, name: str, taken: set[str]) -> str:
    """`name`, or `stem (2).ext`, `stem (3).ext`... the first not on disk and not already used by this plan."""
    if name not in taken and not (folder / name).exists():
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    n = 2
    while True:
        candidate = f"{stem} ({n}){ext}"
        if candidate not in taken and not (folder / candidate).exists():
            return candidate
        n += 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_plan(
    root: Path,
    *,
    now: float | None = None,
    uti_lookup: UtiLookup | None = None,
    safe: Callable[..., str | None] = unsafe_reason,
    settle: float = 0.5,
    max_ops: int = MAX_OPS,
) -> Plan:
    """Plan moves for the top-level regular files of *root*. Nothing is moved or written."""
    root = Path(root).resolve(strict=True)
    current = time.time() if now is None else now
    plan = Plan(root=str(root), created_at=current)
    taken: dict[Path, set[str]] = {}
    sizes: dict[int, list[Path]] = {}
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        path = Path(entry.path)
        reason = safe(path, now=current, settle=settle)
        if reason is not None:
            plan.skipped.append({"name": display_name(entry.name), "reason": reason})
            continue
        label = classify(entry.name, path=str(path), uti_lookup=uti_lookup)
        folder_name = FOLDERS[label]
        if folder_name is None:
            plan.kept.append(display_name(entry.name))
            continue
        if len(plan.rows) >= max_ops:
            plan.truncated = True
            continue
        st = os.lstat(path)
        top = root / folder_name
        if os.path.lexists(top) and not top.is_dir():
            reason = "destination folder name is taken by a file"
            plan.skipped.append({"name": display_name(entry.name), "reason": reason})
            continue
        folder = top
        if label in MONTH_BUCKETS:
            folder = folder / datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m")
        used = taken.setdefault(folder, set())
        final = _free_name(folder, entry.name, used)
        used.add(final)
        row = Row(str(path), str(folder / final), label, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        same_size = sizes.setdefault(st.st_size, [])
        if same_size:
            mine = _sha256(path)
            row.duplicate_of = next((display_name(p.name) for p in same_size if _sha256(p) == mine), None)
        same_size.append(path)
        plan.rows.append(row)
    return plan


def save_plan(plan: Plan, state_dir: Path, *, now: float | None = None) -> Path:
    """Write plan.json under *state_dir*. One pending plan at a time, and at most one new plan per hour."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    current = time.time() if now is None else now
    pending = state_dir / "plan.json"
    if pending.exists():
        raise PlanLimit("a plan is already pending: approve, execute or discard it first")
    stamp = state_dir / "last-plan-at"
    if stamp.exists() and current - float(stamp.read_text()) < MIN_SECONDS_BETWEEN_PLANS:
        raise PlanLimit("at most one plan per hour")
    pending.write_text(plan.to_json(), encoding="ascii")
    stamp.write_text(str(current))
    return pending


def load_plan(path: Path) -> Plan:
    data = json.loads(Path(path).read_text(encoding="ascii"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"unknown plan schema {data.get('schema')!r}")
    rows = [Row(**r) for r in data.pop("rows")]
    return Plan(rows=rows, **data)
