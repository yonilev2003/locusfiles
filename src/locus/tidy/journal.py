"""Write-ahead journal: one JSON line per state change, flushed and fsynced.

States per row: `intent` (about to move), `done` (moved), `failed` (move refused, nothing changed),
`skipped` (row no longer matched its fingerprint), `undone`. A row whose last state is `intent` is an
interrupted move; `execute.recover` decides from the files on disk whether it happened."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="ascii").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line from a crash: the intent before it is what recovery reads
        return out

    def last_state(self) -> dict[tuple[str, int], dict[str, Any]]:
        """(run id, row index) -> that row's latest record."""
        latest: dict[tuple[str, int], dict[str, Any]] = {}
        for rec in self.records():
            if "row" in rec:
                latest[(rec["run"], rec["row"])] = rec
        return latest
