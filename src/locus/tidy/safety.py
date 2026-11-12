"""Which files are safe to propose moving, and how a file name is shown (draft ADR-082 items 2-3)."""

from __future__ import annotations

import os
import stat as stat_module
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

PARTIAL_SUFFIXES = (".crdownload", ".download", ".part", ".partial", ".tmp", ".opdownload")
MIN_AGE_SECONDS = 10 * 60
SETTLE_SECONDS = 0.5
SF_DATALESS = 0x40000000  # st_flags bit of an iCloud/File Provider placeholder on macOS
DISPLAY_NAME_MAX = 80


def display_name(name: str) -> str:
    """A file name made safe to print or to show to a classifier: control, format (bidi, zero-width) and
    unassigned characters become '?', and the length is capped. Display only: the move uses the real name."""
    cleaned = "".join("?" if unicodedata.category(ch)[0] == "C" else ch for ch in name)
    return cleaned if len(cleaned) <= DISPLAY_NAME_MAX else cleaned[: DISPLAY_NAME_MAX - 1] + "…"


def unsafe_reason(
    path: Path,
    *,
    now: float | None = None,
    settle: float = SETTLE_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
) -> str | None:
    """Why *path* must not be proposed, or None. The order is cheap checks first; the size-changing check
    stats the file twice, `settle` seconds apart."""
    name = path.name
    if name.startswith("."):
        return "hidden file"
    try:
        st = os.lstat(path)
    except OSError as exc:
        return f"cannot stat ({exc.strerror})"
    if stat_module.S_ISLNK(st.st_mode):
        return "symlink"
    if not stat_module.S_ISREG(st.st_mode):
        return "not a regular file (folder, package or special file)"
    if name.lower().endswith(PARTIAL_SUFFIXES):
        return "partial download"
    if st.st_nlink > 1:
        return "hard-linked (more than one name)"
    if getattr(st, "st_flags", 0) & SF_DATALESS:
        return "cloud placeholder (not downloaded)"
    current = time.time() if now is None else now
    if current - st.st_mtime < MIN_AGE_SECONDS:
        return "modified in the last 10 minutes"
    if settle > 0:
        sleeper(settle)
        try:
            again = os.lstat(path)
        except OSError:
            return "disappeared while checking"
        if (again.st_size, again.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            return "still changing"
    return None
