"""One result row for chunks with identical text.

A file copied into three folders, or the same note in a backup and a project, used to fill three of the top
results with the same words. `collapse_duplicates` keeps the best-ranked copy and lists the other paths as
`also_in`, so every path is still shown (a citation can name any of them). Only chunks whose text is the same
once whitespace is normalised collapse; two chunks of one file are never merged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from . import restrictions
from .ingest.store import Hit


def collapse_duplicates(hits: list[Hit]) -> list[Hit]:
    """*hits* in rank order, one row per distinct chunk text; the other paths of a duplicate go to `also_in`."""
    first: dict[str, int] = {}
    rows: list[Hit] = []
    for hit in hits:
        key = " ".join(hit.text.split())
        at = first.get(key) if key else None
        if at is None or hit.path == rows[at].path or hit.path in rows[at].also_in:
            if key and at is None:
                first[key] = len(rows)
            rows.append(hit)
            continue
        rows[at] = replace(rows[at], also_in=(*rows[at].also_in, hit.path))
    return rows


def visible_collapsed_hits(guard: Any, fetch: Callable[[int], list[Hit]], k: int) -> tuple[list[Hit], int]:
    """The first *k* visible hits after collapsing duplicates, and how many ranked hits the folder rules hid.
    When duplicates leave fewer than *k* rows, more are fetched (up to restrictions.OVERFETCH_LIMIT) to fill the
    page. `also_in` lists only copies among the hits fetched. `hidden` counts what was hidden before the last
    kept row of the final fetch, so it can be a little high when a refill happened."""
    n = k
    while True:
        kept, hidden = restrictions.visible_hits(guard, fetch, n)
        rows = collapse_duplicates(kept)
        if len(rows) >= k or len(kept) < n or n >= restrictions.OVERFETCH_LIMIT:
            return rows[:k], hidden
        n = min(n * 3, restrictions.OVERFETCH_LIMIT)
